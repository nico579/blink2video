"""Construit ``dist-xr-tester/Tester-XR.exe`` pour un diagnostic utilisateur."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import build


BASE_DIR = Path(__file__).resolve().parent
VENV = BASE_DIR / "build_venv"
WORK = BASE_DIR / "build-xr-tester"
DIST = BASE_DIR / "dist-xr-tester"
SPEC = BASE_DIR / "xr_tester.spec"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--propre",
        action="store_true",
        help="supprimer uniquement les sorties précédentes du testeur",
    )
    args = parser.parse_args()

    if sys.platform != "win32":
        raise SystemExit("Le testeur direct est actuellement destiné à Windows.")

    if args.propre:
        for path in (WORK, DIST):
            resolved = path.resolve()
            if resolved.parent != BASE_DIR.resolve():
                raise SystemExit(f"Refus de supprimer un chemin inattendu : {resolved}")
            if resolved.exists():
                shutil.rmtree(resolved)

    python = build._python(VENV)
    if not python.exists():
        build.executer(
            [sys.executable, "-m", "venv", str(VENV)],
            "création de l'environnement de construction",
        )
    build.executer(
        [str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        "mise à jour de pip",
    )
    build.executer(
        [str(python), "-m", "pip", "install", "--quiet", *build.PAQUETS],
        "installation des dépendances",
    )
    build.executer(
        [
            str(python), "-m", "PyInstaller", "--noconfirm", "--clean",
            "--distpath", str(DIST), "--workpath", str(WORK), str(SPEC),
        ],
        "construction du testeur XR",
    )

    executable = DIST / "Tester-XR.exe"
    if not executable.is_file():
        raise SystemExit(f"Testeur introuvable : {executable}")
    print(f"\nTesteur construit : {executable}")
    print("À placer dans le même dossier que blink2video.exe, puis double-cliquer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
