# PyInstaller spec for the standalone macOS app.
# Build with: pyinstaller packaging/epic_compliance.spec --distpath dist --workpath build
from pathlib import Path

block_cipher = None
project_root = Path.cwd()

datas = [
    (str(project_root / "rules"), "rules"),
    (str(project_root / "epic_compliance" / "templates"), "epic_compliance/templates"),
    (str(project_root / "epic_compliance" / "static"), "epic_compliance/static"),
]

a = Analysis(
    ["app_entry.py"],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "epic_compliance.api",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Epic Compliance",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="Epic Compliance",
)

app = BUNDLE(
    coll,
    name="Epic Compliance.app",
    icon=None,
    bundle_identifier="com.epiccompliance.app",
    info_plist={
        "CFBundleName": "Epic Compliance",
        "CFBundleShortVersionString": "0.1.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    },
)
