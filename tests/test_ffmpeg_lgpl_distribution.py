from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify_ffmpeg_lgpl_distribution.py"
BUILDER = ROOT / "scripts" / "build_ffmpeg_lgpl_macos.sh"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fake_binary(path: Path, *, mode: str = "lgpl") -> None:
    config = "--disable-gpl"
    license_line = "This version of ffmpeg is licensed under the GNU Lesser General Public License version 2.1 or later"
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
        f"  *-L*) printf '%s\\n' '{license_line}' ;;\n"
        "  *) printf '%s\\n' 'ffmpeg version test' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def compliance(directory: Path, ffmpeg: Path, ffprobe: Path) -> None:
    source_url = "https://github.com/Acrabxie/lumeri/releases/download/ffmpeg-source/ffmpeg-source.tar.xz"
    source_dir = directory / "source"
    source_dir.mkdir(exist_ok=True)
    source = source_dir / "ffmpeg-source.tar.xz"
    build_script = source_dir / "build-ffmpeg-lgpl.sh"
    configure_arguments = source_dir / "configure.args"
    source.write_bytes(b"FFmpeg corresponding source fixture\n")
    build_script.write_text("#!/bin/sh\n# fixture build script\n", encoding="utf-8")
    configure_arguments.write_text("--disable-gpl\n--disable-nonfree\n", encoding="utf-8")
    (directory / "COPYING.LGPL-2.1-or-later").write_text("GNU LESSER GENERAL PUBLIC LICENSE\nVersion 2.1\n", encoding="utf-8")
    (directory / "NOTICE").write_text(f"Lumeri includes FFmpeg. Corresponding source: {source_url}\n", encoding="utf-8")
    (directory / "manifest.json").write_text(json.dumps({
        "schema": "lumeri.ffmpeg-distribution.v1",
        "license": "LGPL-2.1-or-later",
        "sourceBundle": {
            "url": source_url,
            "sha256": digest(source),
            "materials": {
                "source": {"path": "source/ffmpeg-source.tar.xz", "sha256": digest(source)},
                "buildScript": {"path": "source/build-ffmpeg-lgpl.sh", "sha256": digest(build_script)},
                "configureArguments": {"path": "source/configure.args", "sha256": digest(configure_arguments)},
            },
        },
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


builder = BUILDER.read_text(encoding="utf-8")
for required in (
    "ffmpeg-8.1.2.tar.xz",
    "--disable-gpl",
    "--disable-nonfree",
    "--disable-libx264",
    "--disable-libx265",
    "--disable-libfdk-aac",
    "--disable-autodetect",
    "--enable-videotoolbox",
    "LEGAL/FFmpeg/source",
    "verify_ffmpeg_lgpl_distribution.py",
):
    assert required in builder


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

    source = legal / "source" / "ffmpeg-source.tar.xz"
    source.write_bytes(b"tampered corresponding source fixture\n")
    rejected = verify(ffmpeg, ffprobe, legal)
    assert rejected.returncode != 0
    assert "source material SHA-256 differs" in rejected.stderr
