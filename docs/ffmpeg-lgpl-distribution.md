# Bundled FFmpeg LGPL-only distribution policy

Lumeri source remains MIT-licensed. A release may separately distribute an
LGPL-only FFmpeg/ffprobe pair, but it must never distribute a build configured
with `--enable-gpl` or `--enable-nonfree`.

Every DMG that includes `ffmpeg` or `ffprobe` must include
`LEGAL/FFmpeg/` beside those binaries. That directory contains:

- `COPYING.LGPL-2.1-or-later`;
- `NOTICE`, naming FFmpeg and the public corresponding-source URL; and
- `manifest.json`, which binds the exact FFmpeg and ffprobe SHA-256 values to
  that source bundle's URL and SHA-256.

The packaging gate is `scripts/verify_ffmpeg_lgpl_distribution.py`. It
executes both binaries, requires an explicit `--disable-gpl`, rejects
`--enable-gpl`, `--enable-nonfree`, and nonredistributable output, and verifies
the bytes against material shipped with the DMG.

The packaging input is a vendor directory with this shape:

```
bin/ffmpeg
bin/ffprobe
LEGAL/FFmpeg/{manifest.json,COPYING.LGPL-2.1-or-later,NOTICE}
```

macOS and Windows packers must take that directory explicitly; they must not
fall back to `ffmpeg-static` or another opaque package-manager binary.

An LGPL-only FFmpeg build does not include GPL encoders such as `libx264` or
`libx265`. Product code that uses the bundle must select an enabled LGPL or
platform encoder and keep a tested fallback; a successful license gate alone
is not feature acceptance.

The corresponding-source bundle must contain the exact FFmpeg source, build
scripts, configuration, patches, and a checksum manifest. It must exclude GPL
components such as x264, x265, and vid.stab. Hosting or changing that public
source bundle is a release action and requires its own release approval.
