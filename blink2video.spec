# Recette de construction du bundle autonome. Voir build.py, qui prépare
# l'environnement isolé puis appelle PyInstaller sur ce fichier.
#
# Choix du mode : un dossier, pas un fichier unique. L'outil se relance
# lui-même (la surveillance appelle le téléchargement, l'assemblage, puis
# l'interface), et un exécutable « onefile » réextrait la centaine de méga-
# octets du bundle à *chaque* lancement, y compris pour ces relances. Le
# dossier se compresse tout aussi bien pour la diffusion, et démarre
# instantanément.

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


SRC = Path(SPECPATH)
APP_ICON = SRC / "assets" / "blink2video.png"
# Favicon de l'interface web (serve.py, route /favicon.ico) : embarqué sous
# assets/ pour que le chemin lu au démarrage (runtime.resource_dir() /
# "assets" / ...) soit le même, figé ou depuis les sources.
FAVICON = SRC / "assets" / "blink2video.ico"
# Marqueur absent des bundles officiels. L'édition Windows 7 s'en sert pour
# s'identifier et pour refuser une mise à jour vers le runtime Python 3.12.
WIN7_MARKER = SRC / "assets" / "windows7-build.txt"

# ffmpeg est propre à la plateforme : on demande le binaire à imageio_ffmpeg,
# installé dans l'environnement de construction, qui fournit celui de l'hôte.
# À défaut, on reprend la copie déposée dans _vendor. PyInstaller ne sachant
# pas produire pour un autre système que le sien, ce choix se fait à la
# construction et vaut pour la plateforme courante uniquement.
def _ffmpeg() -> str:
    # build.py a déjà choisi un binaire sachant incruster du texte, et l'a
    # désigné ici : c'est lui qui fait autorité.
    import os

    choisi = os.environ.get("BLINK_FFMPEG")
    if choisi:
        return choisi
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        import glob

        trouves = sorted(glob.glob("_vendor/imageio_ffmpeg/binaries/ffmpeg-*"))
        if not trouves:
            raise SystemExit(
                "ffmpeg introuvable : installez imageio-ffmpeg dans "
                "l'environnement de construction."
            )
        return trouves[0]


def _modules() -> list:
    import importlib.util

    charge = importlib.util.spec_from_file_location("runtime", "runtime.py")
    runtime = importlib.util.module_from_spec(charge)
    charge.loader.exec_module(runtime)
    return sorted({verbe.module for verbe in runtime.VERBES.values()}
                  - {runtime.ENTREE} | {"runtime", "tzdata"})


FFMPEG = _ffmpeg()

analysis = Analysis(
    ["blink2video.py"],
    pathex=["."],
    # ffmpeg voyage à la racine du bundle : merge_daily.find_ffmpeg l'y cherche
    # en premier, avant le PATH de la machine cible.
    binaries=[(FFMPEG, ".")],
    # Windows n'a pas de base de fuseaux horaires système : sans ces données,
    # ZoneInfo("Europe/Paris") échoue et tout l'horodatage avec.
    datas=(collect_data_files("tzdata", include_py_files=False)
           + ([(str(APP_ICON), ".")] if APP_ICON.exists() else [])
           + ([(str(FAVICON), "assets")] if FAVICON.exists() else [])
           + ([(str(WIN7_MARKER), ".")]
              if os.environ.get("BLINK_BUILD_TARGET") == "windows7" else [])),
    # Les verbes sont résolus par importlib au moment de l'appel : l'analyse
    # statique de PyInstaller ne peut pas les voir, il faut les nommer.
    # Déduits de runtime.VERBES : les verbes sont résolus par importlib au
    # moment de l'appel, donc invisibles à l'analyse statique. Les énumérer ici
    # à la main créerait une liste parallèle de plus, et c'est exactement ce
    # qui a fait échouer la CI après le renommage de « review » en « serve ».
    hiddenimports=_modules(),
    hookspath=[],
    runtime_hooks=[],
    # imageio_ffmpeg est écarté : son seul intérêt est de fournir un binaire,
    # déjà copié à la racine ci-dessus. Le laisser entrer le ferait embarquer
    # une seconde fois, 84 Mo pour rien. find_ffmpeg trouve celui de la racine
    # avant même de tenter cet import.
    excludes=["tkinter", "PyInstaller", "pytest", "imageio_ffmpeg"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="blink2video",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    # Même mécanisme que lidar2map : PNG source portable, converti par
    # PyInstaller en ressource native sur la plateforme de construction.
    icon=str(APP_ICON),
)

COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="blink2video",
)
