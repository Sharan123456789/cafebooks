# -*- mode: python ; coding: utf-8 -*-
# Build the Windows desktop app from the project root:
#     pip install -r requirements-desktop.txt
#     pyinstaller --noconfirm packaging/CafeBooks.spec
# Output: dist/CafeBooks/CafeBooks.exe  (then build the installer with packaging/CafeBooks.iss)
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent

datas = [
    (str(ROOT / "cafebooks" / "web" / "static"), "cafebooks/web/static"),
    (str(ROOT / "demo_data"), "demo_data"),
]
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("cafebooks")
    + ["sqlalchemy.dialects.sqlite", "openpyxl", "multipart", "python_multipart", "tzdata"]
)

a = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["psycopg", "psycopg_binary", "matplotlib", "customtkinter", "pytest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CafeBooks",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
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
    name="CafeBooks",
)
