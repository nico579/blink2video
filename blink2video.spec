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


def _runtime():
    import importlib.util

    charge = importlib.util.spec_from_file_location("runtime", "runtime.py")
    runtime = importlib.util.module_from_spec(charge)
    charge.loader.exec_module(runtime)
    return runtime


def _modules(runtime) -> list:
    return sorted({verbe.module for verbe in runtime.VERBES.values()}
                  - {runtime.ENTREE} | {"runtime", "tzdata"})


# Ressource VERSIONINFO du binaire Windows. Un PE PyInstaller sans éditeur,
# description ni copyright renseignés ressemble statistiquement aux
# échantillons malveillants des jeux d'entraînement des moteurs antivirus à
# heuristique ML (SentinelOne, Zillya...) : constaté sur les faux positifs
# remontés par un utilisateur sur Reddit, ce champ manquant est un signal
# faible mais réel pour ces classifieurs. Sans effet hors Windows, PyInstaller
# ignore "version=" sur les autres plateformes.
def _version_info(version: str) -> str:
    parties = (version.split(".") + ["0", "0", "0"])[:3]
    tuple_version = tuple(int(p) for p in parties) + (0,)
    chemin = SRC / ".version_info.txt"
    chemin.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={tuple_version},
    prodvers={tuple_version},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [StringTable(
        u'040904B0',
        [StringStruct(u'CompanyName', u'nico579'),
         StringStruct(u'FileDescription', u'blink2video - enregistreur video pour cameras Blink'),
         StringStruct(u'FileVersion', u'{version}'),
         StringStruct(u'InternalName', u'blink2video'),
         StringStruct(u'LegalCopyright', u'GPLv3 - nico579'),
         StringStruct(u'OriginalFilename', u'blink2video.exe'),
         StringStruct(u'ProductName', u'blink2video'),
         StringStruct(u'ProductVersion', u'{version}')])
      ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")
    return str(chemin)


FFMPEG = _ffmpeg()
RUNTIME = _runtime()

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
    hiddenimports=_modules(RUNTIME),
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
    # Sans fenêtre, comme pythonw.exe (déjà substitué à python.exe pour
    # l'exécution depuis les sources, voir autostart.py) : avec console=True,
    # même l'entrée de démarrage automatique d'un exe installé ouvrait une
    # console à chaque connexion, ce que toute l'icône de zone de
    # notification est justement censée éviter (constaté en conditions
    # réelles sur la VM Windows 7, et signalé par un utilisateur sur
    # Reddit). print() ne casse rien sans console : runtime.py compte déjà
    # dessus pour tourner sous pythonw.exe, un chemin déjà éprouvé, pas
    # nouveau avec ce changement.
    console=False,
    # Même mécanisme que lidar2map : PNG source portable, converti par
    # PyInstaller en ressource native sur la plateforme de construction.
    icon=str(APP_ICON),
    version=_version_info(RUNTIME.VERSION),
)

COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="blink2video",
)
