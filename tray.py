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
import raccourci_bureau
import runtime

# Mêmes deux langues que la page web (serve.py, const I18N) ; runtime.lire_langue()
# rapporte celle du dernier chargement de page (POST /api/lang à chaque setLang()),
# pas la locale du système : le menu doit suivre la page, pas l'OS.
LIBELLES = {
    "fr": {"ouvrir": "Ouvrir", "maj": "Mettre à jour vers {version}",
           "redemarrer": "Redémarrer", "arreter": "Arrêter",
           "raccourci": "Créer un raccourci sur le Bureau"},
    "en": {"ouvrir": "Open", "maj": "Update to {version}",
           "redemarrer": "Restart", "arreter": "Stop",
           "raccourci": "Create a Desktop shortcut"},
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


def executer(port: int, arret: threading.Event, nettoyer) -> None:
    """Bloque sur la boucle de l'icone, thread principal exige sous macOS.

    `arret` : leve par l'appelant quand un verbe surveille meurt de
    lui-meme (crash) ; un thread interne referme alors l'icone pour rendre
    la main au nettoyage habituel.

    `nettoyer` : arrete directement, dans ce meme processus, les verbes que
    l'appelant a lances (voir nettoyer_lances(), blink_cli.py). Redemarrer/
    Arreter passaient avant uniquement par un « blink2video restart »
    detache (_relancer) : constate en reel sur Windows 7, l'icone pouvait
    disparaitre (icon.stop() rendant la main) sans que rien ne s'arrete
    vraiment derriere, sans qu'on ait pu etablir pourquoi le second
    processus detache n'aboutissait pas toujours. Appeler nettoyer()
    directement, en synchrone, ne depend plus de ce second processus - la
    seule chose garantie de marcher est un Ctrl+C sur le processus lui-meme
    (bug 6, revue du 27/08), donc ce menu doit produire le meme effet."""
    import pystray
    from PIL import Image

    adresse = f"http://127.0.0.1:{port}/"

    def ouvrir(icon=None, item=None):
        webbrowser.open(adresse)

    def redemarrer(icon, item):
        # nettoyer() en tâche de fond, pas ici : ce callback tourne sur le
        # même thread que la pompe de messages Windows de l'icône, et
        # nettoyer() peut bloquer jusqu'à 15 s (délai de grâce coopératif).
        # Geler ce thread empêchait icon.stop() d'être traité à temps,
        # laissant l'icône elle-même en vie une fois tout le reste arrêté
        # (constaté en réel : « il reste juste le systray »). _relancer()
        # doit néanmoins attendre la fin de nettoyer() - sans quoi le
        # nouveau « start » pourrait tenter de se lier au port avant que
        # l'ancien serve ne l'ait libéré.
        def suite():
            nettoyer()
            _relancer(sans_relance=False)

        threading.Thread(target=suite, daemon=True).start()
        icon.stop()

    def arreter(icon, item):
        threading.Thread(target=nettoyer, daemon=True).start()
        icon.stop()

    def creer_raccourci(icon, item):
        raccourci_bureau.creer()

    def mettre_a_jour(icon, item):
        runtime.demarrer(
            runtime.self_command("update"), cwd=str(runtime.app_dir()),
            stdin=subprocess.DEVNULL,
            stdout=(runtime.app_dir() / "maj.log").open("ab"),
            stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"))
        icon.stop()

    def menu():
        # Un appelable plutot qu'une liste figee : necessaire pour lire
        # runtime.lire_langue()/maj.disponible() a chaque reconstruction.
        # Ca ne suffit pourtant pas seul : le backend win32 de pystray ne
        # rappelle PAS ce generateur a chaque clic droit, il reutilise le
        # HMENU construit une fois pour toutes au demarrage (verifie dans
        # pystray/_win32.py, _on_notify utilise self._menu_handle, jamais
        # regenere sans un appel explicite a icon.update_menu() - documente
        # dans Icon.update_menu() elle-meme : necessaire des que les
        # changements sont "triggered by actions other than the menu item
        # activation callbacks", exactement notre cas). D'ou le thread de
        # rafraichissement plus bas, qui appelle update_menu() en boucle.
        mots = LIBELLES[runtime.lire_langue()]
        yield pystray.MenuItem(mots["ouvrir"], ouvrir, default=True)
        neuve = maj.disponible(reseau=False)
        if neuve:
            yield pystray.MenuItem(mots["maj"].format(version=neuve["version"]),
                                   mettre_a_jour)
        yield pystray.MenuItem(mots["redemarrer"], redemarrer)
        yield pystray.MenuItem(mots["arreter"], arreter)
        yield pystray.MenuItem(mots["raccourci"], creer_raccourci)

    icone_fichier = runtime.resource_dir() / "assets" / "blink2video.ico"
    image = Image.open(str(icone_fichier))

    icon = pystray.Icon("blink2video", image, "blink2video", menu=pystray.Menu(menu))

    def veille():
        arret.wait()
        icon.stop()

    def rafraichir():
        # icon.update_menu() reconstruit le HMENU depuis menu() : sans ce
        # thread, changer de langue ou voir paraitre une mise a jour
        # n'apparaitrait dans le menu qu'apres un redemarrage complet de
        # l'icone (voir le commentaire dans menu()). Cinq secondes : assez
        # court pour paraitre immediat a l'ouverture du menu, assez long
        # pour rester un cout negligeable (reconstruire trois-quatre
        # entrees de menu, pas un travail reseau).
        while not arret.wait(timeout=5):
            try:
                icon.update_menu()
            except Exception:
                pass

    threading.Thread(target=veille, daemon=True).start()
    threading.Thread(target=rafraichir, daemon=True).start()
    icon.run()
