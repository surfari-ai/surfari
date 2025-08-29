# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['surfari/navigation_cli.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('surfari/util/config.json', 'surfari/util'),
        ('surfari/view/html_to_text.js', 'surfari/view'),
        ('surfari/security/.env', 'surfari/security'),
        ('surfari/security/google_client_secret.json', 'surfari/security'),
        ('surfari/security/credentials.db', 'surfari/security'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

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
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='navigation_cli',
)
