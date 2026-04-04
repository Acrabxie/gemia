"""DMG / EXE packaging feasibility validator for Gemia.

Run:
  python feasibility_dmg_exe.py            # check only
  python feasibility_dmg_exe.py --build    # attempt a real PyInstaller build
"""
from __future__ import annotations

import importlib
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    RESULTS.append((label, ok, note))
    mark = "✓" if ok else "✗"
    print(f"  {mark}  {label}" + (f"  — {note}" if note else ""))


def section(title: str) -> None:
    print(f"\n{'─'*50}")
    print(f"  {title}")
    print(f"{'─'*50}")


def main(build: bool = False) -> None:
    print("\n══════════════════════════════════════════════")
    print("  Gemia  ·  DMG / EXE Feasibility Report")
    print("══════════════════════════════════════════════")

    # ── Environment ────────────────────────────────────────────────────
    section("Environment")
    plat = platform.system()
    check("Platform", True, plat)
    check("Python ≥ 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    check("Architecture", True, platform.machine())

    # ── Core deps ──────────────────────────────────────────────────────
    section("Core dependencies")
    for mod in ("certifi", "webview"):
        try:
            importlib.import_module(mod)
            check(mod, True)
        except ImportError:
            check(mod, False, "pip install " + ("pywebview" if mod == "webview" else mod))

    ffmpeg = shutil.which("ffmpeg")
    check("ffmpeg in PATH", bool(ffmpeg), ffmpeg or "not found — bundle separately")

    # ── PyInstaller ────────────────────────────────────────────────────
    section("PyInstaller")
    pyinstaller = shutil.which("pyinstaller")
    check("pyinstaller in PATH", bool(pyinstaller), pyinstaller or "pip install pyinstaller")

    # ── Static assets ─────────────────────────────────────────────────
    section("Project assets")
    check("launcher.py", (ROOT / "launcher.py").exists())
    check("server.py", (ROOT / "server.py").exists())
    check("static/index.html", (ROOT / "static" / "index.html").exists())
    check("skills/ dir", (ROOT / "skills").is_dir())
    check("gemia/ package", (ROOT / "gemia" / "__init__.py").exists())

    # ── macOS-specific ─────────────────────────────────────────────────
    if plat == "Darwin":
        section("macOS — DMG")
        create_dmg = shutil.which("create-dmg")
        check(
            "create-dmg",
            bool(create_dmg),
            create_dmg or "brew install create-dmg  (optional, for polished DMG)",
        )
        dmgbuild = shutil.which("dmgbuild")
        check(
            "dmgbuild",
            bool(dmgbuild),
            dmgbuild or "pip install dmgbuild  (alternative)",
        )
        check(
            "codesign available",
            bool(shutil.which("codesign")),
            "required for Gatekeeper (Developer ID cert needed for distribution)",
        )

    # ── Windows-specific ───────────────────────────────────────────────
    if plat == "Windows":
        section("Windows — EXE")
        check("NSIS (installer wizard)", bool(shutil.which("makensis")),
              "optional — for a polished installer")

    # ── Build attempt ─────────────────────────────────────────────────
    if build and pyinstaller:
        section("Build attempt")
        spec_content = _generate_spec()
        spec_path = ROOT / "Gemia.spec"
        spec_path.write_text(spec_content)
        print(f"  → Spec written to {spec_path}")
        print("  → Running PyInstaller (this may take 1-2 min)…")
        result = subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--noconfirm", str(spec_path)],
            cwd=ROOT,
            capture_output=False,
        )
        built = result.returncode == 0
        check("PyInstaller build", built, "see dist/ directory" if built else "build failed")

        if built and plat == "Darwin" and create_dmg:
            app_path = ROOT / "dist" / "Gemia.app"
            dmg_path = ROOT / "dist" / "Gemia.dmg"
            print("  → Creating DMG with create-dmg…")
            r = subprocess.run([
                "create-dmg",
                "--volname", "Gemia",
                "--window-size", "600", "400",
                "--icon-size", "128",
                "--app-drop-link", "400", "200",
                str(dmg_path), str(app_path),
            ], capture_output=False)
            check("create-dmg", r.returncode == 0, str(dmg_path) if r.returncode == 0 else "failed")
    elif build and not pyinstaller:
        print("\n  ✗  Cannot build — pyinstaller not found.")

    # ── Summary ───────────────────────────────────────────────────────
    section("Summary")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"  {passed}/{total} checks passed")

    if plat == "Darwin":
        print("""
  Recommended macOS workflow:
    1.  python feasibility_dmg_exe.py --build
    2.  brew install create-dmg
    3.  codesign --deep -s "Developer ID Application: ..." dist/Gemia.app
    4.  create-dmg dist/Gemia.dmg dist/Gemia.app
""")
    elif plat == "Windows":
        print("""
  Recommended Windows workflow:
    1.  python feasibility_dmg_exe.py --build
    2.  Distribute dist/Gemia.exe directly, or wrap with NSIS installer.
""")
    else:
        print(f"\n  Platform '{plat}' — use PyInstaller to produce a binary for the target OS.")


def _generate_spec() -> str:
    """Return a PyInstaller .spec file tailored for Gemia."""
    root = ROOT.as_posix()
    return f"""\
# Gemia.spec  — generated by feasibility_dmg_exe.py
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(r"{root}")

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "static"),  "static"),
        (str(ROOT / "skills"),  "skills"),
        (str(ROOT / "gemia"),   "gemia"),
    ] + collect_data_files("certifi"),
    hiddenimports=[
        "gemia",
        "gemia.orchestrator",
        "gemia.ai",
        "gemia.ai.ai_client",
        "gemia.ai.gemini_adapter",
        "certifi",
        "webview",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Gemia",
    console=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="Gemia",
)

app = BUNDLE(
    coll,
    name="Gemia.app",
    bundle_identifier="com.gemia.app",
    info_plist={{
        "NSHighResolutionCapable": True,
        "CFBundleShortVersionString": "0.1.0",
    }},
)
"""


if __name__ == "__main__":
    main(build="--build" in sys.argv)
