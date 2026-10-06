"""Raccourci de bureau : ouvrir ou lancer blink2video en un double-clic.

N'installe rien de permanent, contrairement à autostart.py : un seul fichier
posé sur le Bureau, que l'utilisateur retire lui-même (glisser à la
corbeille) le jour où il n'en veut plus. Pas de pendant « off ».

La pose du fichier (Bureau de chaque système, .lnk, .app, .desktop, marque de
confiance de GNOME) est dans nico579_commons.raccourci, la même pour les
quatre applications. Ne reste ici que ce qui est propre à blink2video : la
commande, le dossier de travail et l'icône.

La commande posée dans le raccourci est simplement « start » : blink_cli
(voir la branche « start » de executer()) se comporte déjà comme « open »
quand une instance écoute déjà sur le port configuré, sans rien relancer.
Le même raccourci sert donc aussi bien à démarrer qu'à rouvrir l'interface
en place. « --open-browser » est ajouté ici : absent de la composition
standard (elle n'ouvre jamais de navigateur toute seule, voir DEFAUT dans
autostart.py), il faut l'ajouter explicitement pour ce raccourci-ci, qui n'a
de sens que si l'interface finit par s'afficher.
"""

import subprocess
import sys
from pathlib import Path

from nico579_commons import raccourci

import autostart
import runtime


def _icone() -> Path:
    """Windows prend le .ico, macOS ignore l'icône. Les bureaux Linux
    affichent un PNG : à la racine du bundle (blink2video.spec l'y pose), ou
    dans assets/ depuis les sources."""
    racine = runtime.resource_dir()
    ico = racine / "assets" / "blink2video.ico"
    if not sys.platform.startswith("linux"):
        return ico
    for candidat in (racine / "blink2video.png", racine / "assets" / "blink2video.png"):
        if candidat.is_file():
            return candidat
    return ico


def _lancer(commande):
    return runtime.lancer(
        list(commande), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True, errors="replace", check=False)


def creer(simulation: bool = False) -> int:
    """Pose le raccourci sur le Bureau ; 0 si c'est fait (ou simulé)."""
    langue = runtime.lire_langue()
    return raccourci.creer(
        "blink2video",
        # autostart.commande() fait exactement ce qu'il faut : self_command, puis
        # substitution de pythonw.exe à python.exe depuis les sources (seule
        # façon d'obtenir un lancement sans aucune console sous Windows).
        autostart.commande(("start", "--open-browser")),
        runtime.app_dir(),
        _icone(),
        "Ouvrir blink2video" if langue == "fr" else "Open blink2video",
        # Exécutable en mode console (console=True dans le .spec) : il ne peut
        # pas se lancer sans fenêtre, seulement réduite d'emblée sous Windows,
        # ou dans un terminal sous Linux.
        terminal=True, reduit=True, simulation=simulation, langue=langue,
        lancer=_lancer,
    )
