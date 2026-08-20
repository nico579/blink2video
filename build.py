"""Construit le bundle autonome, dans un environnement isolé jetable.

Pourquoi un environnement dédié à la construction, distinct de celui
d'exécution : PyInstaller embarque ce qu'il trouve. Construire depuis
l'installation Python courante ferait entrer dans le bundle tout ce qui traîne
dans site-packages, sans rapport avec l'outil. Un environnement neuf ne
contient que les quatre dépendances déclarées, et le résultat est reproductible
d'une machine à l'autre.

Le venv de construction est isolé de celui d'exécution (~/.blink2video/venv) parce
qu'il contient PyInstaller, qui n'a rien à faire dans l'environnement de tous
les jours.

    python build.py            construit
    python build.py --propre   reconstruit tout depuis zéro
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
VENV = BASE_DIR / "build_venv"
PYTHON = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
SORTIE = BASE_DIR / "dist" / "blink2video"

# Dépendances d'exécution, plus PyInstaller. Volontairement non figées : ce
# n'est pas un logiciel distribué à des tiers, et un verrouillage de versions
# demanderait un entretien que personne ne fera.
PAQUETS = ["aiohttp", "blinkpy", "tzdata", "imageio-ffmpeg", "pyinstaller",
           "Pillow", "pystray"]


# Compilation complète de secours, quand celle fournie par imageio-ffmpeg ne
# sait pas incruster de texte. C'est le cas sous Linux, où elle est produite
# sans libfreetype. Variante « gpl » : c'est celle qui embarque libfreetype.
#
# Le nom de fichier compte le numéro de version ffmpeg (« n7.1 », « n8.1»...),
# qui change à chaque nouvelle compilation publiée sous le tag « latest » :
# l'ancien nom devient introuvable (404) sans que le tag lui-même ne bouge.
# C'est ce qui a cassé ce lien (n7.1 n'existe plus, remplacé par n9.0).
# « master-latest », sans numéro, est la variante que BtbN publie justement
# pour ne pas dépendre d'un nom qui se déplace.
FFMPEG_SECOURS = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
                  "ffmpeg-master-latest-linux64-gpl.tar.xz")


def ffmpeg_utilisable() -> str:
    """Choisit le ffmpeg à embarquer, en exigeant qu'il sache écrire du texte.

    L'outil incruste l'heure dans l'image : un ffmpeg sans le filtre drawtext
    le rend inutile. Plutôt que de changer de méthode d'horodatage pour
    contourner une compilation incomplète, on embarque une compilation
    complète. Le rendu reste identique sur les trois systèmes, et il n'y a
    qu'un seul chemin de code à entretenir."""
    import subprocess as sp

    def sait_ecrire(binaire: str) -> bool:
        try:
            sortie = sp.run([binaire, "-hide_banner", "-filters"],
                            stdout=sp.PIPE, stderr=sp.DEVNULL, text=True,
                            errors="replace", timeout=60, check=False).stdout
        except Exception:
            return False
        return "drawtext" in (sortie or "")

    try:
        resultat = sp.run([str(PYTHON), "-c",
                           "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"],
                          stdout=sp.PIPE, text=True, check=True).stdout.strip()
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

    archive = BASE_DIR / "build" / "ffmpeg-linux.tar.xz"
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


def main() -> int:
    import os

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--propre", action="store_true",
                        help="supprimer l'environnement de construction et les "
                             "sorties précédentes avant de commencer")
    args = parser.parse_args()

    if args.propre:
        for dossier in (VENV, BASE_DIR / "build", BASE_DIR / "dist"):
            if dossier.exists():
                print(f"Suppression de {dossier.name}...")
                shutil.rmtree(dossier, ignore_errors=True)

    if not PYTHON.exists():
        executer([sys.executable, "-m", "venv", str(VENV)],
                 "création de l'environnement de construction")
    executer([str(PYTHON), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
             "mise à jour de pip")
    executer([str(PYTHON), "-m", "pip", "install", "--quiet", *PAQUETS],
             "installation des dépendances")
    ffmpeg = ffmpeg_utilisable()
    print(f"\nffmpeg embarqué : {ffmpeg}")
    os.environ["BLINK_FFMPEG"] = ffmpeg
    executer([str(PYTHON), "-m", "PyInstaller", "--noconfirm", "--clean",
              str(BASE_DIR / "blink2video.spec")],
             "construction du bundle")

    executable = SORTIE / ("blink2video.exe" if sys.platform == "win32" else "blink2video")
    # Rappel utile : le bundle produit ne vaut que pour la plateforme courante.
    if not executable.exists():
        raise SystemExit(f"Bundle introuvable : {executable}")

    taille = sum(f.stat().st_size for f in SORTIE.rglob("*") if f.is_file())
    print(f"\nBundle construit : {SORTIE}")
    print(f"  exécutable : {executable.name}")
    print(f"  taille     : {taille / 1024 / 1024:.0f} Mo")
    print("\nLes données (Blink_Clips, Blink_Daily…) se créeront à côté de "
          "l'exécutable,\nou dans le dossier désigné par la variable BLINK_HOME.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
