"""Repères d'exécution, identiques que l'on tourne sur les sources ou en bundle.

Trois questions changent de réponse une fois l'outil figé par PyInstaller, et
chacune casse silencieusement si on ne la traite pas.

Où sont les données ? À côté des sources en développement, à côté de
l'exécutable une fois distribué. Surtout pas dans le dossier temporaire
d'extraction, qui disparaît à la fermeture : on y perdrait l'archive.

Où sont les ressources embarquées, ffmpeg en particulier ? Dans le dossier
d'extraction justement, que PyInstaller désigne par sys._MEIPASS.

Comment se relancer soi-même ? En développement on appelle l'interpréteur avec
un script ; en bundle il n'y a plus de script, seulement l'exécutable et ses
verbes. C'est le piège classique du motif « lanceur + bundle » : un programme
qui se relance par sys.executable + chemin de script fonctionne parfaitement en
développement et échoue une fois figé.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


DEPENDANCES = {
    "aiohttp": "aiohttp",
    "blinkpy": "blinkpy",
    # Windows n'embarque aucune base de fuseaux horaires : sans ce paquet,
    # ZoneInfo("Europe/Paris") échoue et tout l'horodatage avec.
    "tzdata": "tzdata",
}


def bootstrap() -> None:
    """Prépare un environnement Python isolé, puis s'y relance.

    À appeler tout en haut d'un point d'entrée, AVANT les imports de aiohttp ou
    de blinkpy : c'est le problème de l'œuf et de la poule, on ne peut pas
    vérifier des dépendances après avoir échoué à les importer.

    Trois modes, choisis par --bootstrap= ou par la variable BLINK_BOOTSTRAP :
      auto (défaut) : venv dans ~/.blink/venv, créé au besoin, puis relance
      pip           : installation dans l'environnement courant
      none          : aucune installation, on vérifie et on explique

    Sans effet dans un bundle, qui embarque déjà tout."""
    if frozen() or os.environ.get("BLINK_BOOTSTRAP_DONE"):
        return

    mode = os.environ.get("BLINK_BOOTSTRAP", "auto")
    for argument in list(sys.argv[1:]):
        if argument.startswith("--bootstrap="):
            mode = argument.split("=", 1)[1]
            sys.argv.remove(argument)

    manquantes = [pip for module, pip in DEPENDANCES.items()
                  if importlib.util.find_spec(module) is None]

    if mode == "none":
        if manquantes:
            print("Dépendances absentes : " + ", ".join(manquantes))
            print(f"  pip install {' '.join(manquantes)}")
            sys.exit(1)
        return

    if mode == "pip":
        if manquantes:
            _installer(sys.executable, manquantes)
        return

    venv_dir = Path.home() / ".blink" / "venv"
    python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    # Déjà dans le bon environnement : on continue, sans quoi on se relancerait
    # indéfiniment.
    if Path(sys.prefix).resolve() == venv_dir.resolve():
        if manquantes:
            _installer(sys.executable, manquantes)
        return

    if not manquantes and not venv_dir.exists():
        return  # l'environnement courant suffit, inutile d'en créer un

    if not python.exists():
        print(f"Création de l'environnement isolé dans {venv_dir}...")
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        _installer(str(python), list(DEPENDANCES.values()))

    print(f"Relance dans {venv_dir}...")
    os.execve(str(python), [str(python), *sys.argv],
              dict(os.environ, BLINK_BOOTSTRAP_DONE="1"))


def _installer(python: str, paquets: list) -> None:
    print("Installation de : " + ", ".join(paquets))
    subprocess.run([python, "-m", "pip", "install", "--quiet", *paquets], check=True)


def frozen() -> bool:
    """Vrai lorsque le programme tourne depuis un bundle PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Dossier des données : clips, vidéos, registres, journaux.

    On accepte une redéfinition par variable d'environnement pour permettre une
    installation où le programme est en lecture seule et les données ailleurs."""
    forced = os.environ.get("BLINK_HOME")
    if forced:
        return Path(forced).expanduser().resolve()
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """Dossier des ressources embarquées dans le bundle."""
    if frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    return Path(__file__).resolve().parent


def self_command(verb: str, *arguments: str) -> list:
    """Ligne de commande pour se relancer sur un autre verbe.

    Le seul endroit du programme qui connaisse la différence entre les deux
    modes d'exécution. Tout le reste appelle self_command et ignore s'il tourne
    depuis des sources ou depuis un exécutable."""
    if frozen():
        return [sys.executable, verb, *arguments]
    script = {"download": "blink.py", "merge": "merge_daily.py",
              "review": "review.py", "watch": "watch.py",
              "all": "daily.py", "smoketest": "smoketest.py",
              "autostart": "autostart.py"}.get(verb)
    if script is None:
        raise ValueError(f"verbe inconnu : {verb}")
    base = [sys.executable, "-u", str(Path(__file__).resolve().parent / script)]
    # blink.py attend le verbe « download » comme premier argument positionnel ;
    # les autres programmes sont appelés directement, sans verbe.
    return base + ([verb] if script == "blink.py" else []) + list(arguments)
