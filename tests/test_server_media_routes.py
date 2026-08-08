from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from typing import Any

import server
from gemia import accounts


def _patch_account_roots(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(accounts, "CONFIG_PATH", tmp_path / "config.json")
    accounts._PENDING_OAUTH_STATES.clear()


def _claims(sub: str, email: str) -> dict[str, object]:
    return {
        "sub": sub,
        "email": email,
        "email_verified": True,
        "name": email.split("@")[0],
        "picture": f"https://lh3.googleusercontent.com/a/{sub}",
        "aud": "client.apps.googleusercontent.com",
        "iss": "https://accounts.google.com",
        "exp": 4_102_444_800,
    }


def _make_image(path: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=skyblue:s=96x54:d=0.1",
            "-frames:v",
            "1",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    return path


class _TestServer:
    def __enter__(self) -> "_TestServer":
        self.httpd = HTTPServer(("127.0.0.1", 0), server._Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=3)
        self.httpd.server_close()

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str, bytes]:
        request_headers = dict(headers or {})
        request_data = data
        if body is not None:
            request_data = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            self.base_url + path,
            data=request_data,
            method=method,
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, response.headers.get("Content-Type", ""), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def test_media_routes_require_account_without_404(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_INPUTS_DIR", tmp_path / "inputs")

    with _TestServer() as app:
        status, _, raw = app.request("GET", "/media-library/list")
        assert status == 401
        assert json.loads(raw)["error"] == "not signed in"

        status, _, raw = app.request(
            "POST",
            "/upload-media",
            data=b"not-yet-checked",
            headers={"Content-Type": "image/png", "X-Filename": "still.png"},
        )
        assert status == 401
        assert json.loads(raw)["error"] == "not signed in"


def test_upload_media_route_round_trips_asset_library(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_INPUTS_DIR", tmp_path / "inputs")
    monkeypatch.setattr(
        accounts,
        "verify_google_id_token",
        lambda credential, client_id=None: _claims("g-1", "one@example.com"),
    )
    accounts.sign_in_with_google("one")
    source = _make_image(tmp_path / "still.png")

    with _TestServer() as app:
        status, _, raw = app.request(
            "POST",
            "/upload-media",
            data=source.read_bytes(),
            headers={"Content-Type": "image/png", "X-Filename": "still.png"},
        )
        assert status == 200
        payload = json.loads(raw)
        asset = payload["asset"]
        asset_id = asset["asset_id"]
        assert asset["name"] == "still.png"
        assert asset["media_kind"] == "image"
        assert payload["clip"]["assetId"] == asset_id
        assert payload["path"] == asset["source_path"]

        status, _, raw = app.request("GET", "/media-library/list?limit=300")
        assert status == 200
        listed = json.loads(raw)["assets"]
        assert [item["asset_id"] for item in listed] == [asset_id]

        status, content_type, preview = app.request("GET", asset["preview_src"])
        assert status == 200
        assert content_type.startswith("image/png")
        assert preview.startswith(b"\x89PNG")

        status, _, raw = app.request("POST", f"/media-library/{asset_id}/add-to-project", {})
        assert status == 200
        added = json.loads(raw)
        assert added["clip"]["assetId"] == asset_id

        status, _, raw = app.request("DELETE", f"/media-library/{asset_id}?source=ui")
        assert status == 200
        assert json.loads(raw)["asset"]["deleted_at"]

        status, _, raw = app.request("GET", "/media-library/list")
        assert status == 200
        assert json.loads(raw)["assets"] == []


def test_media_routes_do_not_read_across_active_projects(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_INPUTS_DIR", tmp_path / "inputs")
    monkeypatch.setattr(
        accounts,
        "verify_google_id_token",
        lambda credential, client_id=None: _claims("g-project", "project@example.com"),
    )
    accounts.sign_in_with_google("project")
    active = {"project_id": "project-one"}
    monkeypatch.setattr(server, "_active_media_project_id", lambda _account_id: active["project_id"])
    source = _make_image(tmp_path / "project.png")

    with _TestServer() as app:
        status, _, raw = app.request(
            "POST",
            "/upload-media",
            data=source.read_bytes(),
            headers={"Content-Type": "image/png", "X-Filename": "project.png"},
        )
        assert status == 200
        source_asset = json.loads(raw)["asset"]
        assert source_asset["project_id"] == "project-one"

        active["project_id"] = "project-two"
        status, _, raw = app.request("GET", "/media-library/list")
        assert status == 200
        assert json.loads(raw)["assets"] == []
        status, _, _ = app.request("GET", f"/media-library/{source_asset['asset_id']}")
        assert status == 404
        status, _, _ = app.request("GET", source_asset["preview_src"])
        assert status == 404

        status, _, raw = app.request(
            "POST",
            "/media-library/copy",
            {"source_project_id": "project-one", "asset_id": source_asset["asset_id"]},
        )
        assert status == 200
        copied = json.loads(raw)["asset"]
        assert copied["project_id"] == "project-two"
        assert copied["asset_id"] != source_asset["asset_id"]
        status, _, raw = app.request("GET", "/media-library/list")
        assert [item["asset_id"] for item in json.loads(raw)["assets"]] == [copied["asset_id"]]


def test_media_annotation_routes_and_search(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_INPUTS_DIR", tmp_path / "inputs")
    monkeypatch.setattr(
        accounts,
        "verify_google_id_token",
        lambda credential, client_id=None: _claims("g-2", "two@example.com"),
    )
    accounts.sign_in_with_google("two")
    source = _make_image(tmp_path / "still.png")

    with _TestServer() as app:
        status, _, raw = app.request(
            "POST",
            "/upload-media",
            data=source.read_bytes(),
            headers={"Content-Type": "image/png", "X-Filename": "still.png"},
        )
        assert status == 200
        asset_id = json.loads(raw)["asset_id"]

        status, _, raw = app.request(
            "POST",
            f"/media-library/{asset_id}/annotations",
            {
                "scope": "time_range",
                "start_sec": 1,
                "end_sec": 99,
                "label": "good take",
                "note": "use this for the opening",
                "tags": ["opening", "keeper"],
                "category": "cut_candidate",
                "source": "user",
            },
        )
        assert status == 200, raw
        ann = json.loads(raw)["annotation"]
        assert ann["scope"] == "time_range"
        assert ann["label"] == "good take"
        assert ann["end_sec"] >= ann["start_sec"]

        status, _, raw = app.request("GET", f"/media-library/{asset_id}/annotations")
        assert status == 200
        assert [item["annotation_id"] for item in json.loads(raw)["annotations"]] == [ann["annotation_id"]]

        status, _, raw = app.request("GET", "/media-library/list?q=keeper")
        assert status == 200
        listed = json.loads(raw)["assets"]
        assert [item["asset_id"] for item in listed] == [asset_id]
        assert listed[0]["annotation_summary"]["count"] == 1
        assert "keeper" in listed[0]["annotation_summary"]["tags"]

        status, _, raw = app.request(
            "POST",
            f"/media-library/{asset_id}/annotations/{ann['annotation_id']}",
            {"label": "best opening"},
        )
        assert status == 200
        assert json.loads(raw)["annotation"]["label"] == "best opening"

        status, _, raw = app.request("DELETE", f"/media-library/{asset_id}/annotations/{ann['annotation_id']}")
        assert status == 200
        assert json.loads(raw)["annotation"]["annotation_id"] == ann["annotation_id"]

        status, _, raw = app.request("GET", f"/media-library/{asset_id}/annotations")
        assert status == 200
        assert json.loads(raw)["annotations"] == []
