# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hiddenimports = [
    "app",
    "models",
    "desktop",
    "flask",
    "flask_login",
    "flask_sqlalchemy",
    "sqlalchemy",
    "sqlalchemy.dialects.postgresql",
    "psycopg2",
    "psycopg2._psycopg",
    "werkzeug",
    "jinja2",
    "reportlab",
    "reportlab.pdfbase.ttfonts",
    "reportlab.pdfbase.pdfmetrics",
    "openpyxl",
    "webview",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
]

# pywebview discovers its Windows GUI backend dynamically.
hiddenimports += collect_submodules("webview")

a = Analysis(
    ["desktop.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("templates", "templates"),
        ("static", "static"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "numpy",
        "pandas",
        "torch",
        "tensorflow",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="FinanceCollectionManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon="logo.ico",
)
