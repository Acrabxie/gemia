"""First-run onboarding wizard for Gemia / Lumeri.

A fresh install (server OR CLI) has no usable model provider configured. This
module detects that condition and, on an interactive TTY, walks the user through
picking a model provider, supplying the required key(s), optionally choosing a
BYOK search engine and a proxy, then writes ``~/.gemia/config.json``.

Design contracts (see overnight/onboard task spec):

* ADDITIVE / behaviour-preserving — when a usable provider is ALREADY
  configured, nothing here triggers (no prompt, no hang).
* Headless-safe — never block on ``input()`` when stdin is not a TTY. Instead
  print a clear instructions block and signal not-onboarded so the caller exits
  cleanly.
* Testable without a real TTY — :func:`run_onboarding` accepts injectable
  ``input_fn`` / ``output_fn`` (defaulting to ``input`` / ``print``) so tests can
  drive it deterministically. The module reads/writes :data:`CONFIG_PATH`, which
  tests monkeypatch to a temp file.
* Merge, don't clobber — writing config preserves existing keys.

The config keys written here match exactly what the rest of the system reads
(see ``gemia/gemini_client.py`` provider probe and ``gemia/tools/web_search.py``
search-provider resolution).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

# ── Paths ─────────────────────────────────────────────────────────────
# Module-level so tests can monkeypatch CONFIG_PATH to a temp file.
CONFIG_PATH = Path.home() / ".gemia" / "config.json"

# ── Provider / probe maps ─────────────────────────────────────────────
# A provider is "usable" if ANY of these config keys is present & non-empty.
# Mirrors gemini_client._probe_provider() (env vars are handled there at
# runtime; onboarding only governs the config.json side).
LLM_PROVIDER_KEYS = (
    "vertex_project",
    "gemini_api_key",
    "openrouter_api_key",
    "openai_api_key",
    "anthropic_api_key",
)

# Vertex defaults written alongside vertex_project. These mirror the spec's
# KEY MAP so the system's vertex provider path has sensible models/locations.
VERTEX_DEFAULTS = {
    "lumeri_v3_provider": "vertex",
    "lumeri_v3_model": "google/gemini-3.5-flash",
    "lumeri_v3_location": "global",
    "vertex_location": "us-central1",
    "vertex_video_model": "veo-3.0-fast-generate-001",
    "vertex_image_model": "gemini-2.5-flash-image",
    "vertex_audio_model": "lyria-002",
}

DEFAULT_OPENAI_MODEL = "gpt-4o"

# Search engines selectable in the optional BYOK step. Order matters for the
# numbered menu. Each maps to the config key(s) the system reads.
SEARCH_ENGINES = (
    ("tavily", ("tavily_api_key",)),
    ("serper", ("serper_api_key",)),
    ("brave", ("brave_api_key",)),
    ("exa", ("exa_api_key",)),
    ("bing", ("bing_api_key",)),
    ("google_cse", ("google_cse_key", "google_cse_id")),
    # searxng is keyless & self-hosted — its "key" is the instance URL.
    ("searxng", ("searxng_url",)),
)


# ── Config read / write helpers ───────────────────────────────────────
def read_config() -> dict[str, Any]:
    """Return the parsed ``~/.gemia/config.json`` as a dict, or ``{}``.

    Never raises — a missing or malformed file yields an empty dict.
    """
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def merge_write(updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` into the existing config and persist atomically.

    Existing unrelated keys are preserved (merge, not clobber). The file is
    written via a temp file + ``os.replace`` and chmod 600. Returns the merged
    config dict.
    """
    merged = read_config()
    merged.update(updates)
    _atomic_write_json(CONFIG_PATH, merged)
    return merged


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _has_usable_provider(cfg: dict[str, Any]) -> bool:
    return any(str(cfg.get(key) or "").strip() for key in LLM_PROVIDER_KEYS)


def needs_onboarding() -> bool:
    """True if no usable LLM provider is configured.

    Returns True when the config file is missing OR none of the provider keys
    in :data:`LLM_PROVIDER_KEYS` is present & non-empty.
    """
    if not CONFIG_PATH.exists():
        return True
    return not _has_usable_provider(read_config())


# ── Secret masking ────────────────────────────────────────────────────
def mask_secret(value: str) -> str:
    """Mask a secret for echoing: keep a short tail, never the full value."""
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


# ── Headless instructions ─────────────────────────────────────────────
def _instructions_text() -> str:
    """Plain-text block telling a non-interactive user exactly what to write.

    Lists the exact config.json keys per provider, plus the optional search and
    proxy keys. Used in the headless path.
    """
    lines = [
        "",
        "=" * 64,
        "  Gemia / Lumeri is not configured yet (no model provider found).",
        f"  Create {CONFIG_PATH} with a model-provider key. Examples:",
        "=" * 64,
        "",
        "  Pick ONE model provider and add its key(s):",
        "",
        '  vertex (Google Vertex AI):',
        '    {"vertex_project": "<GCP_PROJECT_ID>",',
        '     "lumeri_v3_provider": "vertex",',
        '     "lumeri_v3_model": "google/gemini-3.5-flash",',
        '     "lumeri_v3_location": "global",',
        '     "vertex_location": "us-central1"}',
        "    (auth: needs gcloud ADC, OR a GCP VM service account with the",
        "     'Vertex AI User' role.)",
        "",
        '  gemini (Google AI Studio key):',
        '    {"gemini_api_key": "<KEY>", "lumeri_v3_provider": "gemini"}',
        "",
        '  openrouter:',
        '    {"openrouter_api_key": "<KEY>", "lumeri_v3_provider": "openrouter"}',
        '    (optional: "openrouter_model": "<model>")',
        "",
        '  openai (GPT):',
        '    {"openai_api_key": "<KEY>", "lumeri_v3_provider": "openai"}',
        f'    (optional: "openai_model": "<model>"  default {DEFAULT_OPENAI_MODEL})',
        "",
        '  claude (Anthropic):',
        '    {"anthropic_api_key": "<KEY>", "lumeri_v3_provider": "claude"}',
        "",
        "  OPTIONAL search engine (otherwise DuckDuckGo, no key):",
        '    set "search_provider" to one of:',
        "      searxng     -> searxng_url (keyless, self-hosted; the free default)",
        "      tavily      -> tavily_api_key",
        "      serper      -> serper_api_key",
        "      brave       -> brave_api_key",
        "      exa         -> exa_api_key",
        "      bing        -> bing_api_key",
        "      google_cse  -> google_cse_key + google_cse_id",
        "",
        "  OPTIONAL proxy (for users behind a firewall; leave empty on cloud):",
        '    {"proxy": "http://host:port"}',
        "",
        "  After writing the file, re-run. You can also run interactively:",
        "    python -m gemia setup",
        "=" * 64,
        "",
    ]
    return "\n".join(lines)


def print_instructions(output_fn: Callable[[str], Any] | None = None) -> str:
    """Emit the headless instructions block. Returns the text printed.

    ``output_fn`` defaults to the builtin ``print`` resolved at call-time (so a
    monkeypatched ``builtins.print`` is honoured).
    """
    if output_fn is None:
        output_fn = print
    text = _instructions_text()
    output_fn(text)
    return text


# ── Interactive wizard ────────────────────────────────────────────────
def _prompt(input_fn: Callable[[str], str], output_fn: Callable[[str], Any], message: str) -> str:
    """Print a prompt via output_fn, then read a line via input_fn.

    We route the visible prompt through output_fn (not input's prompt arg) so
    tests that capture output see the prompt text, and scripted input_fns can
    ignore the argument. The read uses an EMPTY prompt so the builtin input()
    does NOT echo the message a second time (that caused the prompt to appear
    twice, e.g. "exa key (exa_api_key):" printed on two lines).
    """
    output_fn(message)
    try:
        return str(input_fn("")).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


_PROVIDER_MENU = (
    ("1", "vertex", "Google Vertex AI (gcloud ADC / GCP VM service account)"),
    ("2", "gemini", "Google AI Studio (gemini_api_key)"),
    ("3", "openrouter", "OpenRouter (openrouter_api_key)"),
    ("4", "openai", "OpenAI / GPT (openai_api_key)"),
    ("5", "claude", "Anthropic Claude (anthropic_api_key)"),
)


def _collect_provider(
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], Any],
) -> dict[str, Any]:
    """Prompt for a provider choice and its required field(s). Returns updates."""
    output_fn("Choose your model provider:")
    for num, _name, desc in _PROVIDER_MENU:
        output_fn(f"  {num}) {desc}")
    choice = _prompt(input_fn, output_fn, "Provider [1-5] (default 1): ") or "1"

    name = "vertex"
    for num, pname, _desc in _PROVIDER_MENU:
        if choice == num or choice.lower() == pname:
            name = pname
            break

    updates: dict[str, Any] = {}
    if name == "vertex":
        project = _prompt(input_fn, output_fn, "GCP project id (vertex_project): ")
        updates["vertex_project"] = project
        updates.update(VERTEX_DEFAULTS)
        output_fn(
            "Note: vertex needs gcloud ADC (run 'gcloud auth application-default "
            "login') OR a GCP VM service account with the 'Vertex AI User' role."
        )
    elif name == "gemini":
        key = _prompt(input_fn, output_fn, "Gemini API key (gemini_api_key): ")
        updates["gemini_api_key"] = key
        updates["lumeri_v3_provider"] = "gemini"
    elif name == "openrouter":
        key = _prompt(input_fn, output_fn, "OpenRouter API key (openrouter_api_key): ")
        updates["openrouter_api_key"] = key
        updates["lumeri_v3_provider"] = "openrouter"
        model = _prompt(input_fn, output_fn, "OpenRouter model (optional, blank to skip): ")
        if model:
            updates["openrouter_model"] = model
    elif name == "openai":
        key = _prompt(input_fn, output_fn, "OpenAI API key (openai_api_key): ")
        updates["openai_api_key"] = key
        updates["lumeri_v3_provider"] = "openai"
        model = _prompt(
            input_fn, output_fn,
            f"OpenAI model (optional, default {DEFAULT_OPENAI_MODEL}): ",
        )
        if model:
            updates["openai_model"] = model
    elif name == "claude":
        key = _prompt(input_fn, output_fn, "Anthropic API key (anthropic_api_key): ")
        updates["anthropic_api_key"] = key
        updates["lumeri_v3_provider"] = "claude"

    return updates


def _collect_search(
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], Any],
) -> dict[str, Any]:
    """Optionally pick a BYOK search engine. Skipping leaves DuckDuckGo."""
    output_fn("")
    output_fn("Optional: pick a search engine (else DuckDuckGo, no key).")
    output_fn("  searxng is free & self-hosted; the others are BYOK paid keys.")
    output_fn("  0) Skip (use DuckDuckGo)")
    for idx, (engine, _keys) in enumerate(SEARCH_ENGINES, start=1):
        output_fn(f"  {idx}) {engine}")
    choice = _prompt(input_fn, output_fn, "Search engine [0 to skip]: ") or "0"

    if choice in ("0", "", "skip", "none"):
        return {}

    selected: tuple[str, tuple[str, ...]] | None = None
    if choice.isdigit():
        i = int(choice)
        if 1 <= i <= len(SEARCH_ENGINES):
            selected = SEARCH_ENGINES[i - 1]
    else:
        for engine, keys in SEARCH_ENGINES:
            if choice.lower() == engine:
                selected = (engine, keys)
                break
    if selected is None:
        output_fn("Unrecognized choice; skipping search setup (DuckDuckGo).")
        return {}

    engine, keys = selected
    updates: dict[str, Any] = {}
    for key in keys:
        if key == "google_cse_id":
            label = "Google CSE id (google_cse_id)"
        elif key == "searxng_url":
            label = "SearXNG instance URL (searxng_url), e.g. http://127.0.0.1:8080"
        else:
            label = f"{engine} key ({key})"
        value = _prompt(input_fn, output_fn, f"{label}: ")
        updates[key] = value
    updates["search_provider"] = engine
    return updates


def _collect_proxy(
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], Any],
) -> dict[str, Any]:
    """Optionally set a proxy. Blank leaves it unset."""
    output_fn("")
    output_fn("Optional: HTTP/SOCKS proxy (for firewalled networks; blank on a cloud box).")
    proxy = _prompt(input_fn, output_fn, "Proxy URL (blank to skip): ")
    if proxy:
        return {"proxy": proxy}
    return {}


def _print_summary(output_fn: Callable[[str], Any], cfg: dict[str, Any]) -> None:
    """Print a success summary, masking secret values."""
    secret_keys = {
        "gemini_api_key", "openrouter_api_key", "openai_api_key", "anthropic_api_key",
        "tavily_api_key", "serper_api_key", "brave_api_key", "exa_api_key",
        "bing_api_key", "google_cse_key",
    }
    output_fn("")
    output_fn("=" * 64)
    output_fn("  Configuration saved.")
    output_fn(f"  File: {CONFIG_PATH}")
    output_fn("=" * 64)
    output_fn(f"  Model provider: {cfg.get('lumeri_v3_provider', '?')}")
    for key in LLM_PROVIDER_KEYS:
        value = str(cfg.get(key) or "").strip()
        if not value:
            continue
        shown = mask_secret(value) if key in secret_keys else value
        output_fn(f"    {key} = {shown}")
    provider = str(cfg.get("search_provider") or "").strip()
    if provider:
        output_fn(f"  Search engine: {provider}")
        for key in dict(SEARCH_ENGINES).get(provider, ()):  # type: ignore[arg-type]
            value = str(cfg.get(key) or "").strip()
            if not value:
                continue
            shown = mask_secret(value) if key in secret_keys else value
            output_fn(f"    {key} = {shown}")
    else:
        output_fn("  Search engine: DuckDuckGo (no key)")
    if str(cfg.get("proxy") or "").strip():
        output_fn(f"  Proxy: {cfg.get('proxy')}")
    output_fn("")
    output_fn("Re-run anytime with: python -m gemia setup")
    output_fn("")


def run_onboarding(
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
) -> dict[str, Any]:
    """Interactive first-run wizard. Returns the merged config dict.

    Uses ONLY ``input_fn`` / ``output_fn`` for IO so it is fully testable
    without a TTY. Steps: welcome banner -> provider choice + required field(s)
    -> optional search engine -> optional proxy -> merge-write config -> masked
    summary.
    """
    output_fn("")
    output_fn("=" * 64)
    output_fn("  Welcome to Gemia / Lumeri — first-run setup")
    output_fn("  Let's connect a model provider so the brain can think.")
    output_fn("=" * 64)
    output_fn("")

    updates: dict[str, Any] = {}
    updates.update(_collect_provider(input_fn, output_fn))
    updates.update(_collect_search(input_fn, output_fn))
    updates.update(_collect_proxy(input_fn, output_fn))

    merged = merge_write(updates)
    _print_summary(output_fn, merged)
    return merged


def ensure_onboarded(interactive: bool | None = None) -> bool:
    """Ensure a usable provider is configured.

    * If already configured (not :func:`needs_onboarding`): return True
      immediately (no prompt, no output).
    * ``interactive`` defaults to ``sys.stdin.isatty()``.
    * If onboarding is needed and interactive: run the wizard, return True.
    * If onboarding is needed and NOT interactive: print the instructions block
      and return False (caller decides whether to exit). Never blocks on input.
    """
    if not needs_onboarding():
        return True

    if interactive is None:
        try:
            import sys

            interactive = bool(sys.stdin.isatty())
        except Exception:
            interactive = False

    if interactive:
        run_onboarding()
        return True

    print_instructions()
    return False


def run_setup() -> int:
    """Re-run the wizard on demand (the CLI 'setup' subcommand entry).

    Always runs interactively when a TTY is present; on a non-TTY it prints the
    instructions block instead of hanging. Returns a process exit code.
    """
    import sys

    try:
        interactive = bool(sys.stdin.isatty())
    except Exception:
        interactive = False

    if interactive:
        run_onboarding()
        return 0

    print_instructions()
    return 1
