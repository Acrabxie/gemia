#!/bin/bash
# SPDX-License-Identifier: MIT
# Build an arm64 macOS FFmpeg/ffprobe vendor directory that passes Lumeri's
# LGPL-only distribution gate. This script never downloads source or publishes
# an artifact: the caller supplies a hash-verified FFmpeg source archive.

set -euo pipefail

FFMPEG_VERSION="8.1.2"
SOURCE_URL="https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz"
SOURCE_SHA256="464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c"

usage() {
  echo "usage: $0 <ffmpeg-8.1.2.tar.xz> <new-vendor-root>" >&2
  exit 64
}

SOURCE_ARCHIVE="${1:-}"
VENDOR_ROOT="${2:-}"
[[ -n "$SOURCE_ARCHIVE" && -n "$VENDOR_ROOT" ]] || usage
[[ -f "$SOURCE_ARCHIVE" && ! -L "$SOURCE_ARCHIVE" ]] || {
  echo "FFmpeg source archive must be a regular non-symlink file." >&2
  exit 1
}
[[ ! -e "$VENDOR_ROOT" && ! -L "$VENDOR_ROOT" ]] || {
  echo "Refusing to overwrite existing vendor root: $VENDOR_ROOT" >&2
  exit 1
}

actual_source_sha="$(/usr/bin/shasum -a 256 "$SOURCE_ARCHIVE" | /usr/bin/awk '{print $1}')"
[[ "$actual_source_sha" == "$SOURCE_SHA256" ]] || {
  echo "FFmpeg source SHA-256 mismatch: $actual_source_sha" >&2
  exit 1
}

SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
VERIFIER="$(cd "$(dirname "$0")" && pwd)/verify_ffmpeg_lgpl_distribution.py"
[[ -f "$VERIFIER" && ! -L "$VERIFIER" ]] || {
  echo "LGPL distribution verifier is missing: $VERIFIER" >&2
  exit 1
}

WORK_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/lumeri-ffmpeg-lgpl.XXXXXX")"
WORK_CREATED=1
cleanup() {
  if [[ "$WORK_CREATED" == "1" && -d "$WORK_ROOT" && ! -L "$WORK_ROOT" ]]; then
    /bin/rm -rf -- "$WORK_ROOT"
  fi
}
trap cleanup EXIT

SOURCE_TREE="$WORK_ROOT/ffmpeg-$FFMPEG_VERSION"
/usr/bin/tar -xf "$SOURCE_ARCHIVE" -C "$WORK_ROOT"
[[ -d "$SOURCE_TREE" && ! -L "$SOURCE_TREE" ]] || {
  echo "Unexpected FFmpeg source layout." >&2
  exit 1
}

CONFIGURE_ARGUMENTS=(
  "--arch=arm64"
  "--target-os=darwin"
  "--enable-static"
  "--disable-shared"
  "--enable-programs"
  "--disable-doc"
  "--disable-debug"
  "--disable-gpl"
  "--disable-version3"
  "--disable-nonfree"
  "--disable-libx264"
  "--disable-libx265"
  "--disable-libfdk-aac"
  "--disable-autodetect"
  "--enable-pic"
  "--enable-pthreads"
  "--enable-bzlib"
  "--enable-lzma"
  "--enable-zlib"
  "--enable-iconv"
  "--enable-avfoundation"
  "--enable-coreimage"
  "--enable-metal"
  "--enable-securetransport"
  "--enable-audiotoolbox"
  "--enable-videotoolbox"
)

printf '%s\n' "${CONFIGURE_ARGUMENTS[@]}" > "$WORK_ROOT/configure.args"
(
  cd "$SOURCE_TREE"
  ./configure "${CONFIGURE_ARGUMENTS[@]}"
  /usr/bin/make -j"$(/usr/sbin/sysctl -n hw.ncpu)"
)
[[ -x "$SOURCE_TREE/ffmpeg" && -x "$SOURCE_TREE/ffprobe" ]] || {
  echo "FFmpeg build did not produce both executable programs." >&2
  exit 1
}

mkdir -p "$VENDOR_ROOT/bin" "$VENDOR_ROOT/LEGAL/FFmpeg/source"
/usr/bin/install -m 0755 "$SOURCE_TREE/ffmpeg" "$VENDOR_ROOT/bin/ffmpeg"
/usr/bin/install -m 0755 "$SOURCE_TREE/ffprobe" "$VENDOR_ROOT/bin/ffprobe"
/usr/bin/install -m 0644 "$SOURCE_TREE/COPYING.LGPLv2.1" \
  "$VENDOR_ROOT/LEGAL/FFmpeg/COPYING.LGPL-2.1-or-later"
/usr/bin/install -m 0644 "$SOURCE_ARCHIVE" \
  "$VENDOR_ROOT/LEGAL/FFmpeg/source/ffmpeg-$FFMPEG_VERSION.tar.xz"
/usr/bin/install -m 0755 "$SCRIPT_PATH" \
  "$VENDOR_ROOT/LEGAL/FFmpeg/source/build-ffmpeg-lgpl-macos.sh"
/usr/bin/install -m 0644 "$WORK_ROOT/configure.args" \
  "$VENDOR_ROOT/LEGAL/FFmpeg/source/configure.args"
printf '%s\n' \
  "Lumeri includes FFmpeg $FFMPEG_VERSION as a separate LGPL-2.1-or-later program." \
  "Corresponding source: $SOURCE_URL" \
  "The exact source archive, build script, and configure arguments are included in source/." \
  > "$VENDOR_ROOT/LEGAL/FFmpeg/NOTICE"

/usr/bin/python3 - "$VENDOR_ROOT" "$FFMPEG_VERSION" "$SOURCE_URL" "$SOURCE_SHA256" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
version, source_url, source_sha256 = sys.argv[2:]

def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()

legal = root / "LEGAL" / "FFmpeg"
materials = {
    "source": legal / "source" / f"ffmpeg-{version}.tar.xz",
    "buildScript": legal / "source" / "build-ffmpeg-lgpl-macos.sh",
    "configureArguments": legal / "source" / "configure.args",
}
manifest = {
    "schema": "lumeri.ffmpeg-distribution.v1",
    "license": "LGPL-2.1-or-later",
    "ffmpegVersion": version,
    "sourceBundle": {
        "url": source_url,
        "sha256": source_sha256,
        "materials": {
            name: {"path": str(path.relative_to(legal)), "sha256": digest(path)}
            for name, path in materials.items()
        },
    },
    "binaries": {
        name: {"sha256": digest(root / "bin" / name)}
        for name in ("ffmpeg", "ffprobe")
    },
}
(legal / "manifest.json").write_text(
    json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
)
PY

PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 "$VERIFIER" \
  --ffmpeg "$VENDOR_ROOT/bin/ffmpeg" \
  --ffprobe "$VENDOR_ROOT/bin/ffprobe" \
  --compliance-dir "$VENDOR_ROOT/LEGAL/FFmpeg"

echo "PASS: created LGPL-only macOS FFmpeg vendor root: $VENDOR_ROOT"
