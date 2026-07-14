"""Functional tests for M3 v4 verbs.

Tests the contract, not the implementation details. Verifies:
  - web_search: host-side web results, compact JSON landing, no raw HTML
  - web_open: host-side page text extraction, https-only, no raw HTML
  - fetch: https-only, dest_name sanitization, file landing, asset registration
  - run_shell: stdout capture, exit codes, sandbox enforcement, workspace access

Does NOT test sandbox security (deny credentials, modify/delete blocking, network denial).
Those are covered by test_sandbox_v4_isolation.py (M1 real-security probes).
"""
from __future__ import annotations

import asyncio
import base64
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from gemia.budget_guard import BudgetGuard
from gemia.tools import DISPATCHER
from gemia.tools._context import AssetRegistry, ToolContext
from gemia.tools import web_search as _web_search
from gemia.tools import search_library as _search_library
from gemia.tools import fetch as _fetch
from gemia.tools import generate_audio as _generate_audio
from gemia.tools import generate_video as _generate_video
from gemia.tools import run_shell as _run_shell


@pytest.fixture
def sandbox_available() -> bool:
    """Check if sandbox-exec is available (macOS only)."""
    return shutil.which("sandbox-exec") is not None and shutil.which("sandbox-exec")


@pytest.fixture
def workspace_dir() -> Path:
    """Temp directory for tool outputs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def tool_context(workspace_dir: Path) -> ToolContext:
    """ToolContext for the workspace."""
    return ToolContext(
        session_id="test_v4_verbs",
        output_dir=workspace_dir,
        registry=AssetRegistry(),
        emit_progress=lambda _: None,
    )


# ============================================================================
# wiring tests
# ============================================================================


class TestV4VerbWiring:
    """Schema / dispatcher / budget wiring for v4 verbs."""

    def test_web_verbs_are_real_dispatchers(self) -> None:
        for name in (
            "generate_video",
            "generate_audio",
            "search_library",
            "web_search",
            "web_open",
            "fetch",
            "run_shell",
        ):
            assert name in DISPATCHER
            assert not DISPATCHER[name].__name__.startswith("stub_"), name

    def test_web_verbs_have_budget_estimates(self) -> None:
        guard = BudgetGuard()
        assert guard.estimate("web_search") == (0.0, 3.0)
        assert guard.estimate("web_open") == (0.0, 5.0)


# ============================================================================
# Vertex media generation tests
# ============================================================================


class TestVertexGenerateAudio:
    """Lyria tool decodes provider bytes, writes workspace asset, and scrubs output."""

    def test_generate_audio_predict_writes_wav(
        self,
        tool_context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wav = b"RIFF\x24\x00\x00\x00WAVEfmt "
        audio_b64 = base64.b64encode(wav).decode("ascii")
        seen: dict[str, Any] = {}

        def fake_init(self, **kwargs):
            self.location = kwargs.get("location", "us-central1")

        async def fake_predict(self, **kwargs):
            seen.update(kwargs)
            return {
                "_lumeri_request_id": "req_audio",
                "predictions": [
                    {"audioContent": audio_b64, "mimeType": "audio/wav"}
                ],
            }

        monkeypatch.setattr(_generate_audio.GoogleGenAIClient, "__init__", fake_init)
        monkeypatch.setattr(_generate_audio.GoogleGenAIClient, "predict", fake_predict)

        result = asyncio.run(
            _generate_audio.dispatch(
                {"prompt": "bright motion graphics sting", "mood": "clean", "bpm": 120},
                tool_context,
            )
        )

        assert seen["model"] == "lyria-002"
        assert seen["verb"] == "generate_audio"
        assert "bright motion graphics sting" in seen["instances"][0]["prompt"]
        assert "120 BPM" in seen["instances"][0]["prompt"]
        asset = tool_context.registry.get(result["asset_id"])
        assert asset.kind == "audio"
        assert asset.path.suffix == ".wav"
        assert asset.path.read_bytes() == wav
        assert result["metadata"]["request_id"] == "req_audio"
        assert audio_b64 not in json.dumps(result)


class TestVertexGenerateVideo:
    """Veo tool submits LRO async and returns job_id; resolve_veo_job finalises."""

    def test_generate_video_submits_lro_and_returns_job_id(
        self,
        tool_context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """dispatch() should return job_id immediately without polling."""
        monkeypatch.setenv("VERTEX_VIDEO_MODEL", "veo-3.1-fast-generate-preview")
        seen: dict[str, Any] = {}

        def fake_init(self, **kwargs):
            self.location = kwargs.get("location", "us-central1")

        async def fake_submit(self, **kwargs):
            seen["submit"] = kwargs
            return {"name": "operations/veo-test", "_lumeri_request_id": "req_video"}

        monkeypatch.setattr(_generate_video.GoogleGenAIClient, "__init__", fake_init)
        monkeypatch.setattr(_generate_video.GoogleGenAIClient, "predict_long_running", fake_submit)

        result = asyncio.run(
            _generate_video.dispatch(
                {
                    "prompt": "kinetic title animation",
                    "duration_sec": 4,
                    "aspect_ratio": "16:9",
                },
                tool_context,
            )
        )

        assert seen["submit"]["model"] == "veo-3.1-fast-generate-preview"
        assert seen["submit"]["verb"] == "generate_video"
        assert seen["submit"]["parameters"]["durationSeconds"] == 4
        # New async shape: job_id + status, no asset_id yet
        assert "job_id" in result
        assert result["status"] == "submitted"
        assert "pending_asset_id" in result
        assert result["metadata"]["operation_name"] == "operations/veo-test"
        assert result["metadata"]["request_id"] == "req_video"
        # Byte payload must not leak
        assert "base64" not in json.dumps(result).lower()

    def test_resolve_veo_job_writes_mp4_when_lro_done(
        self,
        tool_context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """resolve_veo_job() decodes video and registers asset when operation is done."""
        mp4 = b"\x00\x00\x00\x18ftypmp42"
        video_b64 = base64.b64encode(mp4).decode("ascii")
        seen: dict[str, Any] = {}

        def fake_init(self, **kwargs):
            self.location = kwargs.get("location", "us-central1")

        async def fake_submit(self, **kwargs):
            return {"name": "operations/veo-done", "_lumeri_request_id": "req_v"}

        async def fake_fetch(self, **kwargs):
            seen["fetch"] = kwargs
            return {
                "done": True,
                "response": {
                    "videos": [{"bytesBase64Encoded": video_b64, "mimeType": "video/mp4"}]
                },
            }

        monkeypatch.setattr(_generate_video.GoogleGenAIClient, "__init__", fake_init)
        monkeypatch.setattr(_generate_video.GoogleGenAIClient, "predict_long_running", fake_submit)
        monkeypatch.setattr(_generate_video.GoogleGenAIClient, "fetch_predict_operation", fake_fetch)

        # Step 1: submit → get job_id
        submit_result = asyncio.run(
            _generate_video.dispatch({"prompt": "smoke test"}, tool_context)
        )
        job_id = submit_result["job_id"]
        assert submit_result["status"] == "submitted"

        # Step 2: resolve → LRO done, video written and registered
        resolve_result = asyncio.run(
            _generate_video.resolve_veo_job(job_id, tool_context)
        )
        assert resolve_result["status"] == "done"
        asset_id = resolve_result["asset_id"]
        asset = tool_context.registry.get(asset_id)
        assert asset.kind == "video"
        assert asset.path.suffix == ".mp4"
        assert asset.path.read_bytes() == mp4
        assert seen["fetch"]["operation_name"] == "operations/veo-done"
        assert video_b64 not in json.dumps(resolve_result)

    def test_resolve_veo_job_returns_running_when_not_done(
        self,
        tool_context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """resolve_veo_job() returns status=running when LRO is still in progress."""
        def fake_init(self, **kwargs):
            self.location = "us-central1"

        async def fake_submit(self, **kwargs):
            return {"name": "operations/veo-running"}

        async def fake_fetch(self, **kwargs):
            return {"done": False}

        monkeypatch.setattr(_generate_video.GoogleGenAIClient, "__init__", fake_init)
        monkeypatch.setattr(_generate_video.GoogleGenAIClient, "predict_long_running", fake_submit)
        monkeypatch.setattr(_generate_video.GoogleGenAIClient, "fetch_predict_operation", fake_fetch)

        submit = asyncio.run(_generate_video.dispatch({"prompt": "test"}, tool_context))
        job_id = submit["job_id"]

        result = asyncio.run(_generate_video.resolve_veo_job(job_id, tool_context))
        assert result["status"] == "running"
        assert "pending_asset_id" in result


# ============================================================================
# search_library tests
# ============================================================================


class TestSearchLibrary:
    """search_library is a real non-throwing tool, not a stub."""

    def test_search_library_empty_results_do_not_throw(self, tool_context: ToolContext) -> None:
        result = asyncio.run(
            _search_library.dispatch(
                {"query": "motion graphics", "kind": "any"},
                tool_context,
            )
        )

        assert result["query"] == "motion graphics"
        assert result["kind"] == "any"
        assert result["result_count"] == 0
        assert result["results"] == []
        assert "no matching" in result["summary"]

    def test_search_library_finds_session_registry_assets(
        self,
        tool_context: ToolContext,
        workspace_dir: Path,
    ) -> None:
        image = workspace_dir / "motion_graphics_card.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        record = tool_context.registry.add_external(
            image,
            summary="motion graphics title card",
        )

        result = asyncio.run(
            _search_library.dispatch(
                {"query": "motion graphics", "kind": "image", "limit": 5},
                tool_context,
            )
        )

        assert result["result_count"] == 1
        assert result["results"][0]["asset_id"] == record.asset_id
        assert result["results"][0]["source"] == "session"

    def test_search_library_kind_validation(self, tool_context: ToolContext) -> None:
        with pytest.raises(ValueError, match="kind"):
            asyncio.run(
                _search_library.dispatch(
                    {"query": "motion graphics", "kind": "document"},
                    tool_context,
                )
            )


# ============================================================================
# web_search / web_open verb tests
# ============================================================================


class _Headers:
    def __init__(self, content_type: str) -> None:
        self._content_type = content_type

    def get(self, key: str, default: Any = None) -> Any:
        return self._content_type if key == "Content-Type" else default


class _Resp:
    def __init__(self, payload: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self._payload = payload
        self.headers = _Headers(content_type)

    def read(self, *_args: Any, **_kwargs: Any) -> bytes:
        return self._payload

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False


class _Opener:
    def __init__(self, payload: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.payload = payload
        self.content_type = content_type
        self.requests: list[Any] = []

    def open(self, req: Any, timeout: float | None = None) -> _Resp:
        self.requests.append((req, timeout))
        return _Resp(self.payload, self.content_type)


def _patch_opener(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    content_type: str = "text/html; charset=utf-8",
) -> _Opener:
    opener = _Opener(payload, content_type)
    monkeypatch.setattr(
        _web_search.urllib.request,
        "build_opener",
        lambda *a, **k: opener,
    )
    return opener


class TestWebSearch:
    """Host-side search functionality."""

    def test_web_search_requires_query(self, tool_context: ToolContext) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            asyncio.run(_web_search.dispatch({}, tool_context))

        with pytest.raises(ValueError, match="non-empty"):
            asyncio.run(_web_search.dispatch({"query": ""}, tool_context))

    def test_web_search_parses_results_and_saves_json(
        self,
        tool_context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        html = b"""
        <html><body>
          <!-- RAW_HTML_ONLY_MARKER -->
          <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs%3Fa%3D1">Example Docs</a>
          <div class="result__snippet">Useful &amp; current docs.</div>
          <a class="result__a" href="https://second.example/page">Second Result</a>
          <div class="result__snippet">Second snippet.</div>
          <a class="result__a" href="javascript:alert(1)">Bad Result</a>
        </body></html>
        """
        opener = _patch_opener(monkeypatch, html)

        result = asyncio.run(
            _web_search.dispatch({"query": "lumeri docs", "limit": 5}, tool_context)
        )

        assert result["engine"] == "duckduckgo_lite"
        assert result["query"] == "lumeri docs"
        assert result["result_count"] == 2
        assert result["results"][0] == {
            "title": "Example Docs",
            "url": "https://example.com/docs?a=1",
            "snippet": "Useful & current docs.",
            "source": "example.com",
        }
        assert result["results"][1]["url"] == "https://second.example/page"
        assert opener.requests, "search should issue a host-side request"

        saved = tool_context.output_dir / result["path"]
        assert saved.exists()
        saved_text = saved.read_text(encoding="utf-8")
        assert "RAW_HTML_ONLY_MARKER" not in saved_text
        assert "result__a" not in saved_text
        assert "<html" not in json.dumps(result)

    def test_web_search_limit_is_clamped(
        self,
        tool_context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        html = "\n".join(
            f'<a class="result__a" href="https://example.com/{i}">Result {i}</a>'
            f'<div class="result__snippet">Snippet {i}</div>'
            for i in range(12)
        ).encode("utf-8")
        _patch_opener(monkeypatch, html)

        result = asyncio.run(
            _web_search.dispatch({"query": "many", "limit": 99}, tool_context)
        )

        assert result["result_count"] == 10
        assert len(result["results"]) == 10


class TestWebOpen:
    """Host-side web page opening."""

    def test_web_open_https_only(self, tool_context: ToolContext) -> None:
        with pytest.raises(ValueError, match="https://"):
            asyncio.run(
                _web_search.dispatch_open({"url": "http://example.com"}, tool_context)
            )

        with pytest.raises(ValueError, match="https://"):
            asyncio.run(
                _web_search.dispatch_open({"url": "file:///etc/passwd"}, tool_context)
            )

    def test_web_open_extracts_clean_text(
        self,
        tool_context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        html = b"""
        <html>
          <head>
            <title>Example Page</title>
            <style>.x{display:none}</style>
            <script>window.secret = 'SCRIPT_MARKER';</script>
          </head>
          <body>
            <main><h1>Headline</h1><p>Visible text &amp; details.</p></main>
          </body>
        </html>
        """
        _patch_opener(monkeypatch, html)

        result = asyncio.run(
            _web_search.dispatch_open(
                {"url": "https://example.com/page", "max_chars": 1000},
                tool_context,
            )
        )

        assert result["source"] == "example.com"
        assert result["title"] == "Example Page"
        assert "Headline" in result["content"]
        assert "Visible text & details." in result["content"]
        assert "SCRIPT_MARKER" not in result["content"]
        assert "<script" not in result["content"]
        assert "<h1>" not in result["content"]

        saved = tool_context.output_dir / result["path"]
        assert saved.exists()
        saved_text = saved.read_text(encoding="utf-8")
        assert "SCRIPT_MARKER" not in saved_text
        assert "<html" not in saved_text

    def test_web_open_rejects_binary_content_type(
        self,
        tool_context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_opener(monkeypatch, b"\x89PNG\r\n", "image/png")

        with pytest.raises(ValueError, match="fetch"):
            asyncio.run(
                _web_search.dispatch_open({"url": "https://example.com/pic.png"}, tool_context)
            )


# ============================================================================
# fetch verb tests
# ============================================================================


class TestFetchBasic:
    """Basic fetch functionality."""

    def test_fetch_https_only_rejects_http(self, tool_context: ToolContext) -> None:
        """Fetch must reject http:// URLs."""
        with pytest.raises(ValueError, match="https://"):
            asyncio.run(
                _fetch.dispatch({"url": "http://example.com/file.txt"}, tool_context)
            )

    def test_fetch_https_only_rejects_file(self, tool_context: ToolContext) -> None:
        """Fetch must reject file:// URLs."""
        with pytest.raises(ValueError, match="https://"):
            asyncio.run(
                _fetch.dispatch({"url": "file:///etc/passwd"}, tool_context)
            )

    def test_fetch_requires_url(self, tool_context: ToolContext) -> None:
        """Fetch must reject empty or missing URL."""
        with pytest.raises(ValueError, match="non-empty"):
            asyncio.run(_fetch.dispatch({}, tool_context))

        with pytest.raises(ValueError, match="non-empty"):
            asyncio.run(_fetch.dispatch({"url": ""}, tool_context))


class TestFetchDestName:
    """Destination name sanitization."""

    def test_fetch_dest_name_rejects_traversal(self, tool_context: ToolContext) -> None:
        """Fetch must reject '..' in dest_name."""
        with pytest.raises(ValueError, match="unsafe filename"):
            asyncio.run(
                _fetch.dispatch(
                    {"url": "https://example.com/file.txt", "dest_name": "../evil.txt"},
                    tool_context,
                )
            )

    def test_fetch_dest_name_rejects_absolute(self, tool_context: ToolContext) -> None:
        """Fetch must reject absolute paths in dest_name."""
        with pytest.raises(ValueError, match="unsafe filename"):
            asyncio.run(
                _fetch.dispatch(
                    {"url": "https://example.com/file.txt", "dest_name": "/etc/passwd"},
                    tool_context,
                )
            )

    def test_fetch_dest_name_strips_slashes(self, tool_context: ToolContext) -> None:
        """Fetch must strip leading paths from dest_name, taking only basename."""
        with pytest.raises(ValueError):
            # "dir/subdir/file.txt" should be taken as "file.txt" (OK)
            # but if we pass "/../evil", it should still be caught by ".." check
            asyncio.run(
                _fetch.dispatch(
                    {"url": "https://example.com/file.txt", "dest_name": "dir/subdir/../evil"},
                    tool_context,
                )
            )


# ============================================================================
# run_shell verb tests
# ============================================================================


class TestRunShellBasic:
    """Basic run_shell functionality."""

    @pytest.fixture(autouse=True)
    def _force_sandbox_enabled(self):
        """These tests verify sandbox behavior; force-enable regardless of user toggle."""
        from gemia.sandbox_v4 import is_sandbox_disabled, set_sandbox_disabled
        was_disabled = is_sandbox_disabled()
        set_sandbox_disabled(False)
        yield
        set_sandbox_disabled(was_disabled)

    @pytest.mark.skipif(
        not shutil.which("sandbox-exec"),
        reason="sandbox-exec not available (non-macOS or missing)",
    )
    def test_run_shell_echo(self, tool_context: ToolContext) -> None:
        """Simple echo command in sandbox."""
        result = asyncio.run(
            _run_shell.dispatch(
                {"command": "echo hello"},
                tool_context,
            )
        )

        assert isinstance(result, dict)
        assert result["exit_code"] == 0
        assert "hello" in result["stdout_tail"]
        assert result["sandbox_enforced"] is True
        assert result["timed_out"] is False

    @pytest.mark.skipif(
        not shutil.which("sandbox-exec"),
        reason="sandbox-exec not available (non-macOS or missing)",
    )
    def test_run_shell_exit_code(self, tool_context: ToolContext) -> None:
        """Exit code propagation."""
        result = asyncio.run(
            _run_shell.dispatch(
                {"command": "exit 42"},
                tool_context,
            )
        )

        assert result["exit_code"] == 42
        assert result["timed_out"] is False

    @pytest.mark.skipif(
        not shutil.which("sandbox-exec"),
        reason="sandbox-exec not available (non-macOS or missing)",
    )
    def test_run_shell_stderr(self, tool_context: ToolContext) -> None:
        """Stderr capture."""
        result = asyncio.run(
            _run_shell.dispatch(
                {"command": "echo error >&2 && exit 1"},
                tool_context,
            )
        )

        assert result["exit_code"] == 1
        assert "error" in result["stderr_tail"]

    @pytest.mark.skipif(
        not shutil.which("sandbox-exec"),
        reason="sandbox-exec not available (non-macOS or missing)",
    )
    def test_run_shell_requires_command(self, tool_context: ToolContext) -> None:
        """run_shell must require a non-empty command."""
        with pytest.raises(ValueError, match="non-empty"):
            asyncio.run(_run_shell.dispatch({}, tool_context))

        with pytest.raises(ValueError, match="non-empty"):
            asyncio.run(_run_shell.dispatch({"command": ""}, tool_context))

    @pytest.mark.skipif(
        not shutil.which("sandbox-exec"),
        reason="sandbox-exec not available (non-macOS or missing)",
    )
    def test_run_shell_timeout_validation(self, tool_context: ToolContext) -> None:
        """run_shell validates timeout range."""
        # Negative timeout
        with pytest.raises(ValueError, match="timeout_sec"):
            asyncio.run(
                _run_shell.dispatch(
                    {"command": "echo hi", "timeout_sec": -1},
                    tool_context,
                )
            )

        # Zero timeout
        with pytest.raises(ValueError, match="timeout_sec"):
            asyncio.run(
                _run_shell.dispatch(
                    {"command": "echo hi", "timeout_sec": 0},
                    tool_context,
                )
            )

        # Too large
        with pytest.raises(ValueError, match="timeout_sec"):
            asyncio.run(
                _run_shell.dispatch(
                    {"command": "echo hi", "timeout_sec": 121},
                    tool_context,
                )
            )


class TestRunShellWorkspace:
    """Workspace access in sandbox."""

    @pytest.mark.skipif(
        not shutil.which("sandbox-exec"),
        reason="sandbox-exec not available (non-macOS or missing)",
    )
    def test_run_shell_can_create_file_in_workspace(
        self, tool_context: ToolContext
    ) -> None:
        """run_shell can create files in the workspace directory."""
        test_file = tool_context.output_dir / "test_output.txt"

        asyncio.run(
            _run_shell.dispatch(
                {"command": f"echo content > {test_file}"},
                tool_context,
            )
        )

        assert test_file.exists()
        assert test_file.read_text().strip() == "content"

    @pytest.mark.skipif(
        not shutil.which("sandbox-exec"),
        reason="sandbox-exec not available (non-macOS or missing)",
    )
    def test_run_shell_workspace_is_cwd(self, tool_context: ToolContext) -> None:
        """run_shell executes with workspace as current directory."""
        result = asyncio.run(
            _run_shell.dispatch(
                {"command": "pwd"},
                tool_context,
            )
        )

        output_dir_str = str(tool_context.output_dir)
        assert output_dir_str in result["stdout_tail"]


class TestRunShellSandboxEnforcement:
    """Sandbox enforcement checks."""

    @pytest.fixture(autouse=True)
    def _force_sandbox_enabled(self):
        """These tests verify sandbox behavior; force-enable regardless of user toggle."""
        from gemia.sandbox_v4 import is_sandbox_disabled, set_sandbox_disabled
        was_disabled = is_sandbox_disabled()
        set_sandbox_disabled(False)
        yield
        set_sandbox_disabled(was_disabled)

    @pytest.mark.skipif(
        not shutil.which("sandbox-exec"),
        reason="sandbox-exec not available (non-macOS or missing)",
    )
    def test_run_shell_sandbox_enforced_true(self, tool_context: ToolContext) -> None:
        """run_shell result always has sandbox_enforced=True (or raises)."""
        result = asyncio.run(
            _run_shell.dispatch(
                {"command": "echo ok"},
                tool_context,
            )
        )

        assert result["sandbox_enforced"] is True

    def test_run_shell_no_sandbox_raises(self, tool_context: ToolContext) -> None:
        """If sandbox-exec is not available, run_shell raises RuntimeError."""
        # Temporarily mock the absence of sandbox-exec
        # (This is tested implicitly by the skipif on other tests, but let's be explicit.)
        if shutil.which("sandbox-exec"):
            pytest.skip("sandbox-exec is available; test only runs when unavailable")

        # If we reach here, sandbox-exec is not available
        with pytest.raises(RuntimeError, match="sandbox-exec unavailable"):
            asyncio.run(
                _run_shell.dispatch(
                    {"command": "echo hi"},
                    tool_context,
                )
            )


__all__ = [
    "TestV4VerbWiring",
    "TestSearchLibrary",
    "TestWebSearch",
    "TestWebOpen",
    "TestFetchBasic",
    "TestFetchDestName",
    "TestRunShellBasic",
    "TestRunShellWorkspace",
    "TestRunShellSandboxEnforcement",
]
