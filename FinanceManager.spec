# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.building.build_main import Analysis, PYZ, EXE, COLLECT

block_cipher = None

hiddenimports = (
    collect_submodules("flask")
    + collect_submodules("flask_login")
    + collect_submodules("flask_sqlalchemy")
    + collect_submodules("sqlalchemy")
    + collect_submodules("jinja2")
    + collect_submodules("werkzeug")
    + collect_submodules("reportlab")
    + collect_submodules("openpyxl")
    + collect_submodules("dotenv")
    + collect_submodules("psycopg2")      # Change to "psycopg" if using psycopg v3
    + collect_submodules("webview")
)

datas = [
    ("templates", "templates"),
    ("static", "static"),
]

a = Analysis(
    ["desktop.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=2,
)

pyz = PYZ(
    a.pure,
)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FinanceManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon="static/images/logo.ico",
    version="version_info.txt",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="FinanceManager",
)