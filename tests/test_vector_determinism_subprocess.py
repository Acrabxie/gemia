"""G2 (charter §7) — subprocess determinism gate for ``vector_motion``.

The in-process per-seed test (``test_vector_creative.py::
test_build_scene_is_deterministic_per_seed``) cannot catch nondeterminism that
is stable *within* one interpreter: dict/set iteration order under a single
``PYTHONHASHSEED``, module-level caches, an id counter seeded once per process.
Charter G2 requires a SUBPROCESS double-run — build the same brief in two fresh
interpreters started with DIFFERENT hash seeds and assert the compiled SVG is
byte-identical. Green here certifies the CONTENT layer is a pure function of
the brief, not of process-local state (P5).

Pairs with an anti-triviality guard: the subprocess build must equal the real
in-process build, so the gate exercises the production path, not a divergent
snippet.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import textwrap

# The exact valid brief the in-process determinism test uses (test_vector_
# creative.py::_brief) — a non-trivial scene: logo text + ring mark, playful
# reveal over 4s, seed 11.
_BRIEF_REPR = (
    '{"subject": {"kind": "logo_text", "text": "Lumeri", "mark": "ring"}, '
    '"intent": "reveal", "style": "playful", "duration": 4.0, "seed": 11}'
)

_BUILD_SNIPPET = textwrap.dedent(
    f"""
    import json, sys
    from lumenframe.vector import api
    from lumenframe.vector.svg import compile_scene

    brief = json.loads('{_BRIEF_REPR}')
    sys.stdout.write(compile_scene(api.build_scene(brief)["scene"]))
    """
)


def _build_svg_in_subprocess(hash_seed: str) -> str:
    """Compile the fixed brief to SVG in a FRESH interpreter under the given
    ``PYTHONHASHSEED`` (inherits the venv/editable install via os.environ so
    ``lumenframe`` imports regardless of cwd)."""
    proc = subprocess.run(
        [sys.executable, "-c", _BUILD_SNIPPET],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"subprocess build failed (PYTHONHASHSEED={hash_seed}):\n{proc.stderr}"
    )
    assert proc.stdout, "subprocess produced no SVG output"
    return proc.stdout


def test_build_scene_svg_is_byte_identical_across_subprocess_hash_seeds() -> None:
    """Same brief, two fresh interpreters, DIFFERENT hash seeds → byte-equal
    SVG. Catches dict/set-ordering nondeterminism the in-process test can't."""
    svg_a = _build_svg_in_subprocess("0")
    svg_b = _build_svg_in_subprocess("1")
    assert svg_a == svg_b, (
        "vector_motion CONTENT layer is not deterministic across process / "
        "hash-seed boundaries (charter G2): the compiled SVG differs. "
        f"len(a)={len(svg_a)} sha={hashlib.sha256(svg_a.encode()).hexdigest()[:12]} ; "
        f"len(b)={len(svg_b)} sha={hashlib.sha256(svg_b.encode()).hexdigest()[:12]}"
    )


def test_subprocess_build_matches_in_process_build() -> None:
    """Anti-triviality: the subprocess build equals the in-process production
    build, so the G2 gate exercises the real path, not a divergent snippet."""
    from lumenframe.vector import api
    from lumenframe.vector.svg import compile_scene

    brief = {
        "subject": {"kind": "logo_text", "text": "Lumeri", "mark": "ring"},
        "intent": "reveal",
        "style": "playful",
        "duration": 4.0,
        "seed": 11,
    }
    in_process = compile_scene(api.build_scene(brief)["scene"])
    subprocess_svg = _build_svg_in_subprocess("0")
    assert in_process == subprocess_svg, (
        "subprocess build diverged from the in-process production build — the "
        "G2 gate must exercise the same code path it certifies"
    )
