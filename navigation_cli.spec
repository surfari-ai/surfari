# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from pathlib import Path
import tomllib  # Python 3.11+ (use `tomli` for Python 3.10)
from PyInstaller.utils.hooks import copy_metadata

# --- Load package-data from pyproject.toml ---
datas = []
try:
    with open("pyproject.toml", "rb") as f:
        pyproject = tomllib.load(f)

    pkg_data = pyproject.get("tool", {}).get("setuptools", {}).get("package-data", {})
    for pkg, patterns in pkg_data.items():
        for pattern in patterns:
            src_path = Path("src") / pkg / pattern  # actual dev location
            if src_path.exists():
                target_dir = f"{pkg}/{os.path.dirname(pattern)}"
                datas.append((str(src_path), target_dir))
            else:
                print(f"⚠️ Missing package-data file: {src_path}")
except Exception as e:
    print(f"⚠️ Could not parse pyproject.toml for package-data: {e}")

# --- Ensure fastmcp + mcp metadata are bundled ---
datas += copy_metadata("fastmcp")
datas += copy_metadata("mcp")

a = Analysis(
    ['src/surfari/navigation_cli.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "fastmcp",
        "mcp",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

if sys.platform == "darwin":
    CODESIGN_IDENTITY = os.environ.get("CODESIGN_IDENTITY", None)
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='navigation_cli',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=True,
        codesign_identity=CODESIGN_IDENTITY,
        entitlements_file='installers/entitlements.plist'
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='navigation_cli',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=True,
    )

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name='navigation_cli',
)
