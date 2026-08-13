#!/bin/bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: build_opencv_lgpl_macos.sh <opencv-source-root> <ffmpeg-vendor-root> <python> <build-root> <site-packages-root>

Build a fresh arm64 OpenCV Python binding that links only to the supplied
LGPL-only FFmpeg shared-library vendor. Both output roots must be new,
non-symlink paths; failed builds are intentionally retained for inspection.
EOF
  exit 2
}

if [[ $# -ne 5 ]]; then
  usage
fi

OPENCV_SOURCE_ROOT="$1"
FFMPEG_VENDOR_ROOT="$2"
PYTHON_BIN="$3"
BUILD_ROOT="$4"
SITE_PACKAGES_ROOT="$5"

for path in "$OPENCV_SOURCE_ROOT" "$FFMPEG_VENDOR_ROOT"; do
  if [[ ! -e "$path" || -L "$path" ]]; then
    echo "Required input is missing or is a symlink: $path" >&2
    exit 1
  fi
done
if [[ ! -f "$OPENCV_SOURCE_ROOT/CMakeLists.txt" || ! -x "$PYTHON_BIN" ]]; then
  echo "OpenCV source root or Python executable is invalid." >&2
  exit 1
fi
if [[ ! -x "$FFMPEG_VENDOR_ROOT/bin/ffmpeg" || ! -x "$FFMPEG_VENDOR_ROOT/bin/ffprobe" || \
      ! -d "$FFMPEG_VENDOR_ROOT/LEGAL/FFmpeg" || ! -d "$FFMPEG_VENDOR_ROOT/lib/pkgconfig" ]]; then
  echo "The shared LGPL FFmpeg vendor is incomplete." >&2
  exit 1
fi
if [[ "$BUILD_ROOT" == *' '* || "$SITE_PACKAGES_ROOT" == *' '* ]]; then
  echo "OpenCV output roots must not contain whitespace." >&2
  exit 1
fi
if [[ -e "$BUILD_ROOT" || -L "$BUILD_ROOT" || -e "$SITE_PACKAGES_ROOT" || -L "$SITE_PACKAGES_ROOT" ]]; then
  echo "OpenCV output roots must be new and must not overwrite an earlier build." >&2
  exit 1
fi

for tool in cmake ninja otool install_name_tool ditto; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Required tool is unavailable: $tool" >&2
    exit 1
  fi
done

SCRIPT_ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" "$SCRIPT_ROOT/verify_ffmpeg_lgpl_distribution.py" \
  --ffmpeg "$FFMPEG_VENDOR_ROOT/bin/ffmpeg" \
  --ffprobe "$FFMPEG_VENDOR_ROOT/bin/ffprobe" \
  --compliance-dir "$FFMPEG_VENDOR_ROOT/LEGAL/FFmpeg"

PYTHON_INCLUDE="$($PYTHON_BIN -c 'import sysconfig; print(sysconfig.get_path("include"))')"
PYTHON_NUMPY_INCLUDE="$($PYTHON_BIN -c 'import numpy; print(numpy.get_include())')"
PYTHON_LIBRARY="$($PYTHON_BIN -c 'import os, sysconfig; print(os.path.join(sysconfig.get_config_var("LIBDIR"), sysconfig.get_config_var("LDLIBRARY")))')"
PYTHON_VERSION="$($PYTHON_BIN -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
PYTHON_MAJOR="$($PYTHON_BIN -c 'import sys; print(sys.version_info.major)')"
PYTHON_MINOR="$($PYTHON_BIN -c 'import sys; print(sys.version_info.minor)')"
PYTHON_NUMPY_VERSION="$($PYTHON_BIN -c 'import numpy; print(numpy.version.version)')"
if [[ -z "$PYTHON_INCLUDE" || -z "$PYTHON_NUMPY_INCLUDE" || ! -f "$PYTHON_LIBRARY" ]]; then
  echo "Python development headers or NumPy headers are unavailable." >&2
  exit 1
fi

mkdir -p "$BUILD_ROOT" "$SITE_PACKAGES_ROOT"
FFMPEG_CMAKE_MODULE_ROOT="$BUILD_ROOT/cmake"
mkdir -p "$FFMPEG_CMAKE_MODULE_ROOT"
cat > "$FFMPEG_CMAKE_MODULE_ROOT/FindFFMPEG.cmake" <<'CMAKE'
set(_lumeri_ffmpeg_root "$ENV{LUMERI_FFMPEG_VENDOR_ROOT}")
if(NOT IS_DIRECTORY "${_lumeri_ffmpeg_root}")
  message(FATAL_ERROR "LUMERI_FFMPEG_VENDOR_ROOT must name the verified FFmpeg vendor root")
endif()

find_path(FFMPEG_INCLUDE_DIR NAMES libavcodec/avcodec.h
  PATHS "${_lumeri_ffmpeg_root}/include" NO_DEFAULT_PATH)
foreach(_lumeri_component AVCODEC AVFORMAT AVUTIL SWSCALE AVDEVICE)
  string(TOLOWER "${_lumeri_component}" _lumeri_lower)
  string(REGEX REPLACE "^lib" "" _lumeri_short "${_lumeri_lower}")
  find_library(FFMPEG_${_lumeri_component}_LIBRARY
    NAMES "${_lumeri_short}" "${_lumeri_lower}"
    PATHS "${_lumeri_ffmpeg_root}/lib" NO_DEFAULT_PATH)
endforeach()

include(FindPackageHandleStandardArgs)
find_package_handle_standard_args(FFMPEG REQUIRED_VARS
  FFMPEG_INCLUDE_DIR
  FFMPEG_AVCODEC_LIBRARY
  FFMPEG_AVFORMAT_LIBRARY
  FFMPEG_AVUTIL_LIBRARY
  FFMPEG_SWSCALE_LIBRARY)
if(FFMPEG_FOUND)
  set(FFMPEG_INCLUDE_DIRS "${FFMPEG_INCLUDE_DIR}")
  set(FFMPEG_LIBRARIES
    "${FFMPEG_AVCODEC_LIBRARY}"
    "${FFMPEG_AVFORMAT_LIBRARY}"
    "${FFMPEG_AVUTIL_LIBRARY}"
    "${FFMPEG_SWSCALE_LIBRARY}")
  if(FFMPEG_AVDEVICE_LIBRARY)
    list(APPEND FFMPEG_LIBRARIES "${FFMPEG_AVDEVICE_LIBRARY}")
  endif()
  execute_process(COMMAND "${_lumeri_ffmpeg_root}/bin/ffmpeg" -hide_banner -version
    OUTPUT_VARIABLE _lumeri_ffmpeg_versions ERROR_QUIET)
  foreach(_lumeri_library libavcodec libavformat libavutil libswscale libavdevice)
    string(REGEX MATCH "${_lumeri_library}[ \\t]+([0-9]+)[ \\.]+([0-9]+)[ \\.]+([0-9]+)"
      _lumeri_version_match "${_lumeri_ffmpeg_versions}")
    if(_lumeri_version_match)
      set(FFMPEG_${_lumeri_library}_VERSION
        "${CMAKE_MATCH_1}.${CMAKE_MATCH_2}.${CMAKE_MATCH_3}")
    endif()
  endforeach()
endif()
CMAKE
export LUMERI_FFMPEG_VENDOR_ROOT="$FFMPEG_VENDOR_ROOT"
export CMAKE_PREFIX_PATH="$FFMPEG_VENDOR_ROOT"
export DYLD_LIBRARY_PATH="$FFMPEG_VENDOR_ROOT/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"

cmake -S "$OPENCV_SOURCE_ROOT" -B "$BUILD_ROOT" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_OSX_ARCHITECTURES=arm64 \
  -DCMAKE_MODULE_PATH="$FFMPEG_CMAKE_MODULE_ROOT" \
  -DCMAKE_INSTALL_PREFIX="$SITE_PACKAGES_ROOT/_install" \
  -DBUILD_SHARED_LIBS=OFF \
  -DBUILD_LIST=core,imgproc,imgcodecs,photo,features2d,flann,calib3d,objdetect,ml,dnn,video,videoio,highgui,stitching,gapi \
  -DBUILD_opencv_apps=OFF \
  -DBUILD_opencv_python3=ON \
  -DBUILD_TESTS=OFF \
  -DBUILD_PERF_TESTS=OFF \
  -DBUILD_EXAMPLES=OFF \
  -DBUILD_JAVA=OFF \
  -DOPENCV_ENABLE_NONFREE=OFF \
  -DWITH_FFMPEG=ON \
  -DOPENCV_FFMPEG_USE_FIND_PACKAGE=FFMPEG \
  -DOPENCV_FFMPEG_ENABLE_LIBAVDEVICE=ON \
  -DWITH_AVFOUNDATION=OFF \
  -DWITH_GSTREAMER=OFF \
  -DWITH_OPENCL=OFF \
  -DWITH_IPP=OFF \
  -DWITH_TBB=OFF \
  -DWITH_CUDA=OFF \
  -DWITH_EIGEN=OFF \
  -DWITH_OPENEXR=OFF \
  -DPYTHON3_EXECUTABLE="$PYTHON_BIN" \
  -DPYTHON3INTERP_FOUND=TRUE \
  -DPYTHON3LIBS_FOUND=TRUE \
  -DPYTHON3_VERSION_STRING="$PYTHON_VERSION" \
  -DPYTHON3LIBS_VERSION_STRING="$PYTHON_VERSION" \
  -DPYTHON3_VERSION_MAJOR="$PYTHON_MAJOR" \
  -DPYTHON3_VERSION_MINOR="$PYTHON_MINOR" \
  -DPYTHON3_INCLUDE_PATH="$PYTHON_INCLUDE" \
  -DPYTHON3_INCLUDE_DIR="$PYTHON_INCLUDE" \
  -DPYTHON3_LIBRARY="$PYTHON_LIBRARY" \
  -DPYTHON3_LIBRARIES="$PYTHON_LIBRARY" \
  -DPYTHON3_NUMPY_INCLUDE_DIRS="$PYTHON_NUMPY_INCLUDE" \
  -DPYTHON3_NUMPY_VERSION="$PYTHON_NUMPY_VERSION" \
  -DOPENCV_PYTHON3_INSTALL_PATH="$SITE_PACKAGES_ROOT" \
  | tee "$BUILD_ROOT/configure.log"

if ! grep -Eq 'FFMPEG:[[:space:]]+YES' "$BUILD_ROOT/configure.log"; then
  echo "OpenCV configuration did not enable the approved FFmpeg vendor." >&2
  exit 1
fi

cmake --build "$BUILD_ROOT" --target opencv_python3 --parallel "${LUMERI_OPENCV_JOBS:-4}"
cmake --install "$BUILD_ROOT" --component python

CV2_EXTENSION="$(find "$SITE_PACKAGES_ROOT/cv2" -type f -name 'cv2*.so' -print -quit)"
if [[ -z "$CV2_EXTENSION" ]]; then
  echo "OpenCV Python extension was not installed." >&2
  exit 1
fi

CV2_DYLIB_ROOT="$SITE_PACKAGES_ROOT/cv2/.dylibs"
/usr/bin/ditto "$FFMPEG_VENDOR_ROOT/lib" "$CV2_DYLIB_ROOT"

rewrite_cv2_linkage() {
  local linked base
  while read -r linked _; do
    case "$linked" in
      @rpath/libav*.dylib|@rpath/libsw*.dylib)
        base="${linked##*/}"
        install_name_tool -change "$linked" "@loader_path/.dylibs/$base" "$CV2_EXTENSION"
        ;;
    esac
  done < <(otool -L "$CV2_EXTENSION" | tail -n +2)
}

rewrite_dylib_linkage() {
  local library="$1" linked base
  while read -r linked _; do
    case "$linked" in
      @rpath/libav*.dylib|@rpath/libsw*.dylib)
        base="${linked##*/}"
        install_name_tool -change "$linked" "@loader_path/$base" "$library"
        ;;
    esac
  done < <(otool -L "$library" | tail -n +2)
}

rewrite_cv2_linkage
while IFS= read -r -d '' library; do
  rewrite_dylib_linkage "$library"
done < <(find "$CV2_DYLIB_ROOT" -maxdepth 1 -type f -name '*.dylib' -print0)

if otool -L "$CV2_EXTENSION" | tail -n +2 | grep -Eq '@rpath/(libav|libsw)'; then
  echo "OpenCV extension still has unresolved FFmpeg rpath linkage." >&2
  exit 1
fi
if find "$CV2_DYLIB_ROOT" -maxdepth 1 -type f -name '*.dylib' -print0 | \
    xargs -0 -n1 otool -L | tail -n +2 | grep -Eq '@rpath/(libav|libsw)'; then
  echo "Bundled FFmpeg shared libraries still have unresolved rpath linkage." >&2
  exit 1
fi
if find "$SITE_PACKAGES_ROOT" -type f \( -name 'libx264*.dylib' -o -name 'libx265*.dylib' -o -path '*/imageio_ffmpeg/binaries/*' \) -print -quit | grep -q .; then
  echo "Refusing GPL media payload in the custom OpenCV site-packages output." >&2
  exit 1
fi

env -i PATH="/usr/bin:/bin:/usr/sbin:/sbin" PYTHONPATH="$SITE_PACKAGES_ROOT" "$PYTHON_BIN" - <<'PY'
import cv2

info = cv2.getBuildInformation()
if "FFMPEG:                      YES" not in info:
    raise SystemExit("OpenCV runtime does not report FFmpeg support")
if "Non-free algorithms:         NO" not in info:
    raise SystemExit("OpenCV runtime does not report nonfree algorithms disabled")
print(cv2.__file__)
print(cv2.__version__)
PY

mkdir -p "$SITE_PACKAGES_ROOT/LEGAL"
/usr/bin/ditto "$FFMPEG_VENDOR_ROOT/LEGAL/FFmpeg" "$SITE_PACKAGES_ROOT/LEGAL/FFmpeg"
printf 'PASS: OpenCV Python was built against the supplied LGPL FFmpeg shared libraries.\n'
printf 'site-packages=%s\ncv2=%s\n' "$SITE_PACKAGES_ROOT" "$CV2_EXTENSION"
