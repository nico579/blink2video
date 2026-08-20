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
l'instance en cours.

« Mettre a jour », quand une version plus recente existe, passe de meme par
« blink2video update », le mecanisme du bouton de mise a jour de la page web
(serve.py, /api/update). `maj.disponible(reseau=False)` ne lit que le cache
deja entretenu par le thread de fond de serve.py (six heures de fraicheur,
voir maj.py) : ouvrir le menu n'interroge jamais GitHub soi-meme."""

import os
import subprocess
import threading
import webbrowser

import maj
import runtime

# Mêmes deux langues que la page web (serve.py, const I18N) ; runtime.lire_langue()
# rapporte celle du dernier chargement de page (POST /api/lang à chaque setLang()),
# pas la locale du système : le menu doit suivre la page, pas l'OS.
LIBELLES = {
    "fr": {"ouvrir": "Ouvrir", "maj": "Mettre à jour vers {version}",
           "redemarrer": "Redémarrer", "arreter": "Arrêter"},
    "en": {"ouvrir": "Open", "maj": "Update to {version}",
           "redemarrer": "Restart", "arreter": "Stop"},
}


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

    def mettre_a_jour(icon, item):
        runtime.demarrer(
            runtime.self_command("update"), cwd=str(runtime.app_dir()),
            stdin=subprocess.DEVNULL,
            stdout=(runtime.app_dir() / "maj.log").open("ab"),
            stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"))
        icon.stop()

    def menu():
        # Un appelable plutot qu'une liste figee : pystray le relance a
        # chaque ouverture du menu, donc une version parue pendant que
        # l'icone tournait apparait sans redemarrer quoi que ce soit, et un
        # changement de langue sur la page (relu ici a chaque ouverture)
        # s'applique de la meme facon.
        mots = LIBELLES[runtime.lire_langue()]
        yield pystray.MenuItem(mots["ouvrir"], ouvrir, default=True)
        neuve = maj.disponible(reseau=False)
        if neuve:
            yield pystray.MenuItem(mots["maj"].format(version=neuve["version"]),
                                   mettre_a_jour)
        yield pystray.MenuItem(mots["redemarrer"], redemarrer)
        yield pystray.MenuItem(mots["arreter"], arreter)

    icone_fichier = runtime.resource_dir() / "assets" / "blink2video.ico"
    image = Image.open(str(icone_fichier))

    icon = pystray.Icon("blink2video", image, "blink2video", menu=pystray.Menu(menu))

    def veille():
        arret.wait()
        icon.stop()

    threading.Thread(target=veille, daemon=True).start()
    icon.run()
