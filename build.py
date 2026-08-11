"""Construit le bundle autonome, dans un environnement isolé jetable.

Pourquoi un environnement dédié à la construction, distinct de celui
d'exécution : PyInstaller embarque ce qu'il trouve. Construire depuis
l'installation Python courante ferait entrer dans le bundle tout ce qui traîne
dans site-packages, sans rapport avec l'outil. Un environnement neuf ne
contient que les quatre dépendances déclarées, et le résultat est reproductible
d'une machine à l'autre.

Le venv de construction est isolé de celui d'exécution (~/.blink/venv) parce
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
SORTIE = BASE_DIR / "dist" / "blink"

# Dépendances d'exécution, plus PyInstaller. Volontairement non figées : ce
# n'est pas un logiciel distribué à des tiers, et un verrouillage de versions
# demanderait un entretien que personne ne fera.
PAQUETS = ["aiohttp", "blinkpy", "tzdata", "imageio-ffmpeg", "pyinstaller"]


def executer(commande: list, titre: str) -> None:
    print(f"\n=== {titre}")
    resultat = subprocess.run(commande, cwd=str(BASE_DIR), check=False)
    if resultat.returncode != 0:
        raise SystemExit(f"Échec : {titre}")


def main() -> int:
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
    executer([str(PYTHON), "-m", "PyInstaller", "--noconfirm", "--clean",
              str(BASE_DIR / "blink.spec")],
             "construction du bundle")

    executable = SORTIE / ("blink.exe" if sys.platform == "win32" else "blink")
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
