from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify_ffmpeg_lgpl_distribution.py"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fake_binary(path: Path, *, mode: str = "lgpl") -> None:
    config = "--disable-gpl"
    license_line = "This version of ffmpeg is licensed under the LGPL version 2.1 or later"
    if mode == "gpl":
        config = "--enable-gpl"
        license_line = "This version of ffmpeg is licensed under GPL version 3 or later"
    elif mode == "spoofed_gpl":
        license_line = "This version of ffmpeg is licensed under GPL version 3 or later"
    elif mode == "nonfree":
        config = "--disable-gpl --enable-nonfree"
        license_line = "This version of ffmpeg has nonfree parts compiled in. Therefore it is not legally redistributable."
    path.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        f"  *-buildconf*) printf '%s\\n' 'configuration: {config}' ;;\n"
        f"  *-l*) printf '%s\\n' '{license_line}' ;;\n"
        "  *) printf '%s\\n' 'ffmpeg version test' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def compliance(directory: Path, ffmpeg: Path, ffprobe: Path) -> None:
    source_url = "https://github.com/Acrabxie/lumeri/releases/download/ffmpeg-source/ffmpeg-source.tar.xz"
    (directory / "COPYING.LGPL-2.1-or-later").write_text("GNU LESSER GENERAL PUBLIC LICENSE\nVersion 2.1\n", encoding="utf-8")
    (directory / "NOTICE").write_text(f"Lumeri includes FFmpeg. Corresponding source: {source_url}\n", encoding="utf-8")
    (directory / "manifest.json").write_text(json.dumps({
        "schema": "lumeri.ffmpeg-distribution.v1",
        "license": "LGPL-2.1-or-later",
        "sourceBundle": {"url": source_url, "sha256": "a" * 64},
        "binaries": {
            "ffmpeg": {"sha256": digest(ffmpeg)},
            "ffprobe": {"sha256": digest(ffprobe)},
        },
    }), encoding="utf-8")


def verify(ffmpeg: Path, ffprobe: Path, legal: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([
        sys.executable, str(VERIFIER), "--ffmpeg", str(ffmpeg),
        "--ffprobe", str(ffprobe), "--compliance-dir", str(legal),
    ], capture_output=True, text=True)


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    ffmpeg = root / "ffmpeg"
    ffprobe = root / "ffprobe"
    legal = root / "LEGAL" / "FFmpeg"
    legal.mkdir(parents=True)
    fake_binary(ffmpeg)
    fake_binary(ffprobe)
    compliance(legal, ffmpeg, ffprobe)
    passed = verify(ffmpeg, ffprobe, legal)
    assert passed.returncode == 0, passed.stderr
    for mode, expected in (
        ("gpl", "enables GPL"),
        ("spoofed_gpl", "reports a GPL license"),
        ("nonfree", "enables nonfree"),
    ):
        fake_binary(ffmpeg, mode=mode)
        compliance(legal, ffmpeg, ffprobe)
        rejected = verify(ffmpeg, ffprobe, legal)
        assert rejected.returncode != 0
        assert expected in rejected.stderr
