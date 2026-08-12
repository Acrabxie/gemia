#!/usr/bin/env python3
"""Fail closed unless a bundled FFmpeg pair is LGPL-only and redistributable.

The verifier binds executable bytes to compact compliance material.  Lumeri
itself can therefore remain MIT-licensed while every DMG makes the separately
distributed LGPL FFmpeg source, notices, and binary identity available.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


SCHEMA = "lumeri.ffmpeg-distribution.v1"
LICENSE = "LGPL-2.1-or-later"
REQUIRED_MATERIALS = ("manifest.json", "COPYING.LGPL-2.1-or-later", "NOTICE")


class ComplianceError(ValueError):
    """Raised when a binary or its source/licensing record is incomplete."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ComplianceError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(binary: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            [str(binary), *arguments], check=False, capture_output=True,
            text=True, timeout=20,
        )
    except OSError as error:
        raise ComplianceError(f"cannot execute {binary.name}: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise ComplianceError(f"{binary.name} timed out during compliance inspection") from error
    output = f"{result.stdout}\n{result.stderr}"
    require(result.returncode == 0, f"{binary.name} {' '.join(arguments)} failed: {output.strip()}")
    return output


def read_manifest(directory: Path) -> dict[str, Any]:
    for name in REQUIRED_MATERIALS:
        candidate = directory / name
        require(candidate.is_file() and not candidate.is_symlink(), f"missing required FFmpeg material: {name}")
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ComplianceError(f"invalid FFmpeg manifest: {error}") from error
    require(isinstance(manifest, dict), "FFmpeg manifest must be an object")
    require(manifest.get("schema") == SCHEMA, "unexpected FFmpeg manifest schema")
    require(manifest.get("license") == LICENSE, "FFmpeg manifest must declare LGPL-2.1-or-later")
    source_bundle = manifest.get("sourceBundle")
    require(isinstance(source_bundle, dict), "FFmpeg manifest has no corresponding-source bundle")
    source_url = source_bundle.get("url")
    require(isinstance(source_url, str) and source_url.startswith("https://"), "FFmpeg source bundle must use an HTTPS URL")
    source_sha = source_bundle.get("sha256")
    require(isinstance(source_sha, str) and len(source_sha) == 64 and all(c in "0123456789abcdef" for c in source_sha.lower()), "FFmpeg source bundle needs a SHA-256")
    materials = source_bundle.get("materials")
    require(isinstance(materials, dict), "FFmpeg manifest has no corresponding-source materials")
    for name in ("source", "buildScript", "configureArguments"):
        record = materials.get(name)
        require(isinstance(record, dict), f"FFmpeg manifest lacks source material: {name}")
        relative = record.get("path")
        expected_sha = record.get("sha256")
        require(isinstance(relative, str) and relative, f"FFmpeg source material has no path: {name}")
        parts = Path(relative).parts
        require(not Path(relative).is_absolute() and all(part not in {"", ".", ".."} for part in parts), f"unsafe FFmpeg source material path: {name}")
        require(isinstance(expected_sha, str) and len(expected_sha) == 64 and all(c in "0123456789abcdef" for c in expected_sha.lower()), f"FFmpeg source material needs a SHA-256: {name}")
        candidate = directory.joinpath(*parts)
        require(candidate.is_file() and not candidate.is_symlink(), f"missing bundled FFmpeg source material: {name}")
        require(sha256(candidate) == expected_sha, f"FFmpeg source material SHA-256 differs: {name}")
        if name == "source":
            require(expected_sha.lower() == source_sha.lower(), "FFmpeg source archive does not match sourceBundle SHA-256")
    binaries = manifest.get("binaries")
    require(isinstance(binaries, dict), "FFmpeg manifest has no binary digests")
    for name in ("ffmpeg", "ffprobe"):
        value = binaries.get(name)
        require(isinstance(value, dict), f"FFmpeg manifest lacks {name} record")
        digest = value.get("sha256")
        require(isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest.lower()), f"FFmpeg manifest has invalid {name} SHA-256")
    notice = (directory / "NOTICE").read_text(encoding="utf-8")
    require("FFmpeg" in notice and source_url in notice, "FFmpeg NOTICE must name FFmpeg and the corresponding-source URL")
    license_text = (directory / "COPYING.LGPL-2.1-or-later").read_text(encoding="utf-8")
    require("GNU LESSER GENERAL PUBLIC LICENSE" in license_text and "Version 2.1" in license_text, "FFmpeg LGPL text is incomplete")
    return manifest


def verify_binary(name: str, binary: Path, expected_sha: str) -> None:
    require(binary.is_file() and not binary.is_symlink(), f"{name} must be a regular file")
    require(os.access(binary, os.X_OK), f"{name} is not executable")
    require(sha256(binary) == expected_sha, f"{name} SHA-256 differs from its compliance manifest")
    buildconf = run(binary, "-hide_banner", "-buildconf").lower()
    require("--enable-nonfree" not in buildconf, f"{name} enables nonfree code and is not redistributable")
    require("--enable-gpl" not in buildconf, f"{name} enables GPL code; LGPL-only distribution required")
    require("--disable-gpl" in buildconf, f"{name} build configuration must explicitly disable GPL code")
    license_output = run(binary, "-hide_banner", "-l").lower()
    require("unredistributable" not in license_output and "nonfree" not in license_output, f"{name} reports a nonredistributable license")
    require(re.search(r"(?<!l)gpl(?:\s|$)", license_output) is None, f"{name} reports a GPL license; LGPL-only distribution required")
    require("lgpl" in license_output, f"{name} does not report an LGPL license")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--ffprobe", type=Path, required=True)
    parser.add_argument("--compliance-dir", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        directory = arguments.compliance_dir.resolve(strict=True)
        require(directory.is_dir() and not directory.is_symlink(), "FFmpeg compliance directory must be a real directory")
        manifest = read_manifest(directory)
        binaries = manifest["binaries"]
        verify_binary("ffmpeg", arguments.ffmpeg, binaries["ffmpeg"]["sha256"])
        verify_binary("ffprobe", arguments.ffprobe, binaries["ffprobe"]["sha256"])
    except ComplianceError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("PASS: FFmpeg/ffprobe are LGPL-only and redistributable with bound source material")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
