"""Recette PyInstaller du testeur XR autonome, en un seul fichier."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata


SRC = Path(SPECPATH)
APP_ICON = SRC / "assets" / "blink2video.png"
# Généré explicitement par build_xr_tester.py : le fichier est ignoré par Git
# et n'existe donc jamais dans un checkout propre.
VERSION_INFO = SRC / ".version_info.txt"

analysis = Analysis(
    ["diagnostic_xr.py"],
    pathex=["."],
    binaries=[],
    datas=(collect_data_files("certifi") + copy_metadata("blinkpy")),
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PyInstaller", "imageio_ffmpeg", "PIL", "pystray"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="Tester-XR",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(APP_ICON),
    version=str(VERSION_INFO),
)
