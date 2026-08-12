#!/bin/bash
# SPDX-License-Identifier: MIT
#
# Compatibility entrypoint for an LGPL-only FFmpeg distribution. Existing
# Lumeri tools historically requested libx264/libx265. Those encoders are GPL
# components and must never be required by an MIT Lumeri DMG. On macOS, retry
# with VideoToolbox first; if that host encoder is unavailable, use FFmpeg's
# built-in LGPL MPEG-4 encoder instead.

set -u

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REAL_FFMPEG="$SCRIPT_DIR/ffmpeg-lgpl"

if [[ ! -x "$REAL_FFMPEG" ]]; then
  echo "Lumeri LGPL FFmpeg binary is missing: $REAL_FFMPEG" >&2
  exit 127
fi

hardware=()
fallback=()
translated=0

while (($#)); do
  if [[ "$1" == "-c:v" && $# -ge 2 ]]; then
    case "$2" in
      libx264)
        hardware+=("-c:v" "h264_videotoolbox" "-allow_sw" "1")
        fallback+=("-c:v" "mpeg4" "-q:v" "2")
        translated=1
        shift 2
        continue
        ;;
      libx265)
        hardware+=("-c:v" "hevc_videotoolbox" "-allow_sw" "1")
        fallback+=("-c:v" "mpeg4" "-q:v" "2")
        translated=1
        shift 2
        continue
        ;;
    esac
  fi

  # x264/x265-specific rate control has no equivalent in VideoToolbox or
  # MPEG-4. The original commands place these after -c:v, which is where the
  # translation is detected above.
  if [[ "$translated" == "1" && ( "$1" == "-crf" || "$1" == "-preset" ) && $# -ge 2 ]]; then
    shift 2
    continue
  fi
  # hvc1 is an HEVC-only tag and must not be left on the MPEG-4 fallback.
  if [[ "$translated" == "1" && "$1" == "-tag:v" && $# -ge 2 && "$2" == "hvc1" ]]; then
    shift 2
    continue
  fi

  hardware+=("$1")
  fallback+=("$1")
  shift
done

if [[ "$translated" != "1" ]]; then
  exec "$REAL_FFMPEG" "${hardware[@]}"
fi

if "$REAL_FFMPEG" "${hardware[@]}"; then
  exit 0
fi
hardware_status=$?
if (( hardware_status >= 128 )); then
  exit "$hardware_status"
fi
exec "$REAL_FFMPEG" "${fallback[@]}"
