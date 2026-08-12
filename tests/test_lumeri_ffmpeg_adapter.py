from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "scripts" / "lumeri-ffmpeg-adapter.sh"


def fake_ffmpeg(path: Path) -> None:
    path.write_text(
        "#!/bin/bash\n"
        "set -u\n"
        "printf '%s\\n' \"$*\" >> \"$LUMERI_ADAPTER_LOG\"\n"
        "for arg in \"$@\"; do\n"
        "  if [[ \"$arg\" == 'h264_videotoolbox' || \"$arg\" == 'hevc_videotoolbox' ]]; then exit 9; fi\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    adapter = root / "ffmpeg"
    real = root / "ffmpeg-lgpl"
    log = root / "commands.log"
    adapter.write_bytes(ADAPTER.read_bytes())
    adapter.chmod(0o755)
    fake_ffmpeg(real)
    environment = {**os.environ, "LUMERI_ADAPTER_LOG": str(log)}

    fallback = subprocess.run(
        [str(adapter), "-y", "-i", "input.mov", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "output.mp4"],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert fallback.returncode == 0, fallback.stderr
    hardware, software = log.read_text(encoding="utf-8").splitlines()
    assert "h264_videotoolbox" in hardware
    assert "libx264" not in hardware and "-preset" not in hardware and "-crf" not in hardware
    assert "mpeg4" in software and "libx264" not in software

    log.unlink()
    direct = subprocess.run([str(adapter), "-version"], env=environment, capture_output=True, text=True)
    assert direct.returncode == 0, direct.stderr
    assert log.read_text(encoding="utf-8").strip() == "-version"
