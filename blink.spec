# Recette de construction du bundle autonome. Voir build.py, qui prépare
# l'environnement isolé puis appelle PyInstaller sur ce fichier.
#
# Choix du mode : un dossier, pas un fichier unique. L'outil se relance
# lui-même (la surveillance appelle le téléchargement, l'assemblage, puis
# l'interface), et un exécutable « onefile » réextrait la centaine de méga-
# octets du bundle à *chaque* lancement, y compris pour ces relances. Le
# dossier se compresse tout aussi bien pour la diffusion, et démarre
# instantanément.

from PyInstaller.utils.hooks import collect_data_files

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


FFMPEG = _ffmpeg()

analysis = Analysis(
    ["blink.py"],
    pathex=["."],
    # ffmpeg voyage à la racine du bundle : merge_daily.find_ffmpeg l'y cherche
    # en premier, avant le PATH de la machine cible.
    binaries=[(FFMPEG, ".")],
    # Windows n'a pas de base de fuseaux horaires système : sans ces données,
    # ZoneInfo("Europe/Paris") échoue et tout l'horodatage avec.
    datas=collect_data_files("tzdata", include_py_files=False),
    # Les verbes sont résolus par importlib au moment de l'appel : l'analyse
    # statique de PyInstaller ne peut pas les voir, il faut les nommer.
    hiddenimports=["merge_daily", "review", "watch", "daily", "runtime", "tzdata"],
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
    name="blink",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="blink",
)
