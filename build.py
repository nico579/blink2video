"""Construit le bundle autonome dans un environnement isolé jetable.

Pourquoi un environnement dédié à la construction, distinct de celui
d'exécution : PyInstaller embarque ce qu'il trouve. Construire depuis
l'installation Python courante ferait entrer dans le bundle tout ce qui traîne
dans site-packages, sans rapport avec l'outil.

Le profil normal suit les dépendances courantes. Le profil ``--win7`` est une
cible legacy séparée et reproductible : CPython 3.8.10 x64, dépendances figées
et roue blinkpy 0.25.9 dont seules les métadonnées sont rétroportées.

    python build.py                   construit le bundle normal
    python build.py --propre          le reconstruit depuis zéro
    python build.py --win7 --propre   construit le candidat Windows 7
"""

import argparse
import inspect
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SPEC = BASE_DIR / "blink2video.spec"

# Profil ordinaire : versions courantes et chemins historiques inchangés.
PAQUETS = [
    "aiohttp", "blinkpy", "certifi", "tzdata", "imageio-ffmpeg",
    "pyinstaller", "Pillow", "pystray",
    'backports.zoneinfo; python_version < "3.9"',
]

# Profil Windows 7 : jamais mélangé au venv ni aux sorties officielles.
WIN7_PYTHON = (3, 8, 10)
WIN7_VENV = BASE_DIR / "build_venv_win7"
WIN7_WORK = BASE_DIR / "build-win7"
WIN7_DIST = BASE_DIR / "dist-win7"
WIN7_REQUIREMENTS = BASE_DIR / "requirements-win7.txt"


# Compilation complète de secours, quand celle fournie par imageio-ffmpeg ne
# sait pas incruster de texte. C'est le cas sous Linux, où elle est produite
# sans libfreetype. Variante « gpl » : c'est celle qui embarque libfreetype.
FFMPEG_SECOURS = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-linux64-gpl.tar.xz"
)


def _chemins(win7: bool) -> tuple:
    if win7:
        return WIN7_VENV, WIN7_WORK, WIN7_DIST
    return BASE_DIR / "build_venv", BASE_DIR / "build", BASE_DIR / "dist"


def _python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def verifier_interpreteur_win7() -> None:
    """Refuse un runtime dont les DLL ne sont pas celles garanties pour Win7."""
    erreurs = []
    if sys.platform != "win32":
        erreurs.append("la construction doit tourner sous Windows")
    if sys.version_info[:3] != WIN7_PYTHON:
        erreurs.append(
            "CPython 3.8.10 exact est requis "
            f"(reçu {sys.version_info.major}.{sys.version_info.minor}."
            f"{sys.version_info.micro})"
        )
    if struct.calcsize("P") != 8:
        erreurs.append("un interpréteur x86-64 est requis")
    if sys.implementation.name != "cpython":
        erreurs.append("l'interpréteur doit être CPython")
    if erreurs:
        raise SystemExit("Build Windows 7 refusé : " + "; ".join(erreurs) + ".")


def verifier_python_win7(python: Path) -> None:
    """Refuse aussi un ancien venv Win7 créé avec un autre interpréteur."""
    programme = (
        "import struct,sys; "
        "print('%d.%d.%d|%d|%s' % "
        "(sys.version_info.major, sys.version_info.minor, sys.version_info.micro, "
        "struct.calcsize('P') * 8, sys.implementation.name))"
    )
    attendu = ".".join(str(partie) for partie in WIN7_PYTHON) + "|64|cpython"
    try:
        resultat = subprocess.run(
            [str(python), "-c", programme],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError as erreur:
        raise SystemExit(
            f"Build Windows 7 refusé : environnement illisible ({erreur}). "
            "Relancer avec --propre."
        ) from erreur
    obtenu = (resultat.stdout or "").strip()
    if resultat.returncode != 0 or obtenu != attendu:
        detail = obtenu or (resultat.stderr or "").strip() or "identité inconnue"
        raise SystemExit(
            f"Build Windows 7 refusé : l'environnement utilise {detail}, "
            f"attendu {attendu}. Relancer avec --propre."
        )


def ffmpeg_utilisable(python: Path, travail: Path) -> str:
    """Choisit le ffmpeg à embarquer, en exigeant qu'il sache écrire du texte."""

    def sait_ecrire(binaire: str) -> bool:
        try:
            sortie = subprocess.run(
                [binaire, "-hide_banner", "-filters"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                errors="replace",
                timeout=60,
                check=False,
            ).stdout
        except Exception:
            return False
        return "drawtext" in (sortie or "")

    try:
        resultat = subprocess.run(
            [str(python), "-c",
             "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:
        resultat = ""

    if resultat and sait_ecrire(resultat):
        return resultat

    if sys.platform != "linux":
        raise SystemExit(
            "Aucun ffmpeg capable d'incruster du texte n'a été trouvé, et aucune "
            "solution de secours n'est prévue pour cette plateforme."
        )

    print("\n=== ffmpeg fourni sans drawtext, récupération d'une compilation complète")
    import tarfile
    import urllib.request

    archive = travail / "ffmpeg-linux.tar.xz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        urllib.request.urlretrieve(FFMPEG_SECOURS, archive)
    with tarfile.open(archive) as fichier:
        membre = next(m for m in fichier.getmembers() if m.name.endswith("/bin/ffmpeg"))
        membre.name = "ffmpeg"
        fichier.extract(membre, archive.parent)
    binaire = archive.parent / "ffmpeg"
    binaire.chmod(0o755)
    if not sait_ecrire(str(binaire)):
        raise SystemExit("La compilation de secours ne sait pas non plus écrire du texte.")
    return str(binaire)


def executer(commande: list, titre: str) -> None:
    print(f"\n=== {titre}")
    resultat = subprocess.run(commande, cwd=str(BASE_DIR), check=False)
    if resultat.returncode != 0:
        raise SystemExit(f"Échec : {titre}")


def installer_win7(python: Path, travail: Path) -> None:
    """Installe le verrou Python 3.8 et la roue blinkpy reconditionnée."""
    from build_blinkpy_win7 import WIN7_WHEEL

    executer(
        [str(python), "-m", "pip", "install", "--quiet", "--upgrade",
         "pip==25.0.1"],
        "installation de pip compatible Python 3.8",
    )
    executer(
        [str(python), "-m", "pip", "install", "--quiet", "--requirement",
         str(WIN7_REQUIREMENTS)],
        "installation du verrou Windows 7",
    )
    roues = travail / "wheels"
    executer(
        [str(python), str(BASE_DIR / "build_blinkpy_win7.py"), str(roues)],
        "rétroportage des métadonnées blinkpy 0.25.9",
    )
    executer(
        [str(python), "-m", "pip", "install", "--quiet", "--force-reinstall",
         "--no-deps", str(roues / WIN7_WHEEL)],
        "installation de blinkpy pour Windows 7",
    )
    executer([str(python), "-m", "pip", "check"],
             "cohérence des dépendances Windows 7")


def verifier_blinkpy_win7(python: Path) -> None:
    """Prouve que pip n'a ni rétrogradé blinkpy ni perdu son API moderne."""
    from build_blinkpy_win7 import WIN7_VERSION

    programme = """
import compileall
import inspect
from importlib.metadata import distribution, version
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink
from blinkpy.camera import BlinkCamera
from blinkpy.livestream import BlinkLiveStream

attendue = %r
obtenue = version("blinkpy")
assert obtenue == attendue, (obtenue, attendue)
assert inspect.iscoroutinefunction(Blink.start)
for classe, noms in (
    (Blink, ("send_2fa_code", "get_videos_metadata", "do_http_get")),
    (BlinkCamera, ("init_livestream",)),
    (BlinkLiveStream, ("start", "feed", "recv", "stop")),
):
    for nom in noms:
        assert hasattr(classe, nom), "%%s.%%s absent" %% (classe.__name__, nom)
assert "callback" in inspect.signature(Auth.__init__).parameters
racine = distribution("blinkpy").locate_file("blinkpy")
assert compileall.compile_dir(str(racine), quiet=1)
print("blinkpy %%s : API moderne compilée sous Python 3.8" %% obtenue)
""" % WIN7_VERSION
    executer([str(python), "-c", inspect.cleandoc(programme)],
             "validation du rétroportage blinkpy")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--propre",
        action="store_true",
        help="supprimer l'environnement et les sorties de ce profil avant de commencer",
    )
    parser.add_argument(
        "--win7",
        action="store_true",
        help="construire l'édition legacy Windows 7 (CPython 3.8.10 x64)",
    )
    args = parser.parse_args()

    if args.win7:
        verifier_interpreteur_win7()
    venv, travail, dist = _chemins(args.win7)
    python = _python(venv)
    sortie = dist / "blink2video"

    if args.propre:
        for dossier in (venv, travail, dist):
            if dossier.exists():
                print(f"Suppression de {dossier.name}...")
                shutil.rmtree(dossier, ignore_errors=True)

    if not python.exists():
        executer([sys.executable, "-m", "venv", str(venv)],
                 "création de l'environnement de construction")

    if args.win7:
        verifier_python_win7(python)
        installer_win7(python, travail)
        verifier_blinkpy_win7(python)
    else:
        executer([str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
                 "mise à jour de pip")
        executer([str(python), "-m", "pip", "install", "--quiet", *PAQUETS],
                 "installation des dépendances")

    ffmpeg = ffmpeg_utilisable(python, travail)
    print(f"\nffmpeg embarqué : {ffmpeg}")
    os.environ["BLINK_FFMPEG"] = ffmpeg
    os.environ.pop("BLINK_BUILD_TARGET", None)
    if args.win7:
        os.environ["BLINK_BUILD_TARGET"] = "windows7"

    commande = [
        str(python), "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", str(dist), "--workpath", str(travail), str(SPEC),
    ]
    executer(commande, "construction du bundle")

    executable = sortie / ("blink2video.exe" if sys.platform == "win32" else "blink2video")
    if not executable.exists():
        raise SystemExit(f"Bundle introuvable : {executable}")
    if args.win7:
        executer([str(python), str(BASE_DIR / "verify_win7_bundle.py"), str(sortie)],
                 "contrôle statique du bundle Windows 7")

    taille = sum(f.stat().st_size for f in sortie.rglob("*") if f.is_file())
    print(f"\nBundle construit : {sortie}")
    print(f"  exécutable : {executable.name}")
    print(f"  taille     : {taille / 1024 / 1024:.0f} Mo")
    print("\nLes données (Blink_Clips, Blink_Daily…) se créeront à côté de "
          "l'exécutable,\nou dans le dossier désigné par la variable BLINK_HOME.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
