"""Icone de zone de notification pour l'instance « start » : ouvrir,
redemarrer, arreter sans repasser par le terminal ni la page web.

pystray est la bibliotheque standard Python pour ca, cross-plateforme (choisit
automatiquement win32 sous Windows, AppKit sous macOS, AppIndicator/GTK sous
Linux). Sans serveur graphique (SSH, machine headless, conteneur) son import
echoue proprement : on continue alors sans icone, jamais en erreur bloquante,
une machine sans ecran doit continuer a fonctionner. Meme esprit que
`resource_dir()` ou `app_dir()` : degrader plutot que planter.

Redemarrer/Arreter passent par « blink2video restart », deja le mecanisme du
bouton Stop/Appliquer de la page de reglages (serve.py, /api/stop et
/api/reglages) : la meme commande detachee, pas une deuxieme facon de tuer
l'instance en cours."""

import os
import subprocess
import threading
import webbrowser

import runtime


def disponible() -> bool:
    """Faux si pystray ou son image ne peuvent pas etre charges ici :
    bibliotheque absente, ou aucun backend de zone de notification (Linux
    sans AppIndicator/GTK, session sans affichage)."""
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:
        return False
    return True


def _relancer(sans_relance: bool) -> None:
    arguments = ("restart", "--sans-relance") if sans_relance else ("restart",)
    runtime.demarrer(
        runtime.self_command(*arguments), cwd=str(runtime.app_dir()),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"))


def executer(port: int, arret: threading.Event) -> None:
    """Bloque sur la boucle de l'icone, thread principal exige sous macOS.

    `arret` : leve par l'appelant quand un verbe surveille meurt de
    lui-meme (crash) ; un thread interne referme alors l'icone pour rendre
    la main au nettoyage habituel."""
    import pystray
    from PIL import Image

    adresse = f"http://127.0.0.1:{port}/"

    def ouvrir(icon=None, item=None):
        webbrowser.open(adresse)

    def redemarrer(icon, item):
        _relancer(sans_relance=False)
        icon.stop()

    def arreter(icon, item):
        _relancer(sans_relance=True)
        icon.stop()

    icone_fichier = runtime.resource_dir() / "assets" / "blink2video.ico"
    image = Image.open(str(icone_fichier))

    icon = pystray.Icon(
        "blink2video", image, "blink2video",
        menu=pystray.Menu(
            pystray.MenuItem("Ouvrir", ouvrir, default=True),
            pystray.MenuItem("Redemarrer", redemarrer),
            pystray.MenuItem("Arreter", arreter),
        ),
    )

    def veille():
        arret.wait()
        icon.stop()

    threading.Thread(target=veille, daemon=True).start()
    icon.run()
