"""Icone de zone de notification pour l'instance « start » : ouvrir,
mettre a jour, redemarrer, arreter et creer un raccourci sans repasser par
le terminal ni la page web.

Le menu commun aux quatre applications (blink2video, lidar2map, watch2notif,
gpxsolar), son rafraichissement sur le fil principal sous macOS (issue #31)
et le fil qui porte Redemarrer/Arreter/Mettre a jour jusqu'a leur terme
(course du 2026-09-03 : « Redemarrer » arretait sans relancer) viennent de
nico579_commons.tray, nee de ce fichier. Ne restent ici que les actions
propres a blink2video.

pystray (et donc nico579_commons.tray) choisit win32 sous Windows, AppKit
sous macOS, AppIndicator/GTK sous Linux. Sans serveur graphique (SSH,
machine headless, conteneur) ou sans ces paquets (execution depuis les
sources, ou ils ne sont pas requis), on continue sans icone, jamais en
erreur bloquante : degrader plutot que planter, comme resource_dir() ou
app_dir().

Redemarrer/Arreter passent par nettoyer(), fourni par blink_cli.py, qui
arrete directement les verbes lances ; Redemarrer relance ensuite par
« blink2video restart », le mecanisme du bouton de la page de reglages
(serve.py, /api/redemarrer).

« Mettre a jour », quand une version plus recente existe, passe par
« blink2video update », le mecanisme du bouton de mise a jour de la page
(serve.py, /api/update). `maj.disponible()` ne lit que le cache
deja entretenu par le thread de fond de serve.py : ouvrir le menu
n'interroge jamais GitHub soi-meme."""

import os
import subprocess
import threading
import webbrowser

import maj
import raccourci_bureau
import runtime


def disponible() -> bool:
    """Faux si nico579_commons, pystray ou Pillow ne se chargent pas ici :
    paquets absents (sources), ou aucun backend de zone de notification
    (Linux sans AppIndicator/GTK, session sans affichage)."""
    try:
        from nico579_commons import tray as commun
    except Exception:
        return False
    return commun.disponible()


def _relancer(sans_relance: bool) -> None:
    arguments = ("restart", "--sans-relance") if sans_relance else ("restart",)
    runtime.demarrer(
        runtime.self_command(*arguments), cwd=str(runtime.app_dir()),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"))


def _mettre_a_jour() -> None:
    # Détaché : ce processus fait partie de ce que la mise à jour va arrêter.
    runtime.demarrer(
        runtime.self_command("update"), cwd=str(runtime.app_dir()),
        stdin=subprocess.DEVNULL,
        stdout=(runtime.app_dir() / "maj.log").open("ab"),
        stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"))


def _version_disponible():
    return (maj.disponible() or {}).get("version")


def executer(port: int, arret: threading.Event, nettoyer) -> None:
    """Bloque sur la boucle de l'icone, thread principal exige sous macOS.

    `arret` : leve par l'appelant quand un verbe surveille meurt de
    lui-meme (crash) ; l'icone se referme alors pour rendre la main au
    nettoyage habituel.

    `nettoyer` : arrete directement, dans ce meme processus, les verbes que
    l'appelant a lances (voir nettoyer_lances(), blink_cli.py). Redemarrer
    et Arreter l'appellent en synchrone plutot que de s'en remettre a un
    « blink2video restart » detache : constate en reel sur Windows 7, l'icone
    pouvait disparaitre sans que rien ne s'arrete derriere (bug 6, revue du
    27/08). executer() ne rend la main qu'une fois ce travail fini (voir
    nico579_commons.tray.Tray.executer)."""
    from nico579_commons import tray as commun

    adresse = f"http://127.0.0.1:{port}/"

    def redemarrer():
        nettoyer()
        _relancer(sans_relance=False)

    actions = commun.Actions(
        ouvrir=lambda: webbrowser.open(adresse),
        redemarrer=redemarrer,
        arreter=nettoyer,
        version_disponible=_version_disponible,
        mettre_a_jour=_mettre_a_jour,
        creer_raccourci=raccourci_bureau.creer,
        langue=runtime.lire_langue,
    )
    icone = runtime.resource_dir() / "assets" / "blink2video.ico"
    commun.Tray("blink2video", icone, actions, arret=arret).executer()
