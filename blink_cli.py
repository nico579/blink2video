"""Grammaire CLI et supervision : analyse des arguments, verbes délégués,
composition de plusieurs verbes, arrêt, ouverture du navigateur, onboarding.

Extrait de blink2video.py à l'étape 8 (AUDIT-2026-08-13.md, section 20, 8.5).

O-06/8.7/8.8 : importer ce fichier ne doit pas exiger aiohttp ni blinkpy —
`stop`, `open`, `--help` et `update` n'en ont besoin ni l'un ni l'autre. Les
fonctions qui parlent réellement à Blink (`main`, la branche « start » de
`executer`, `accueillir`) importent donc `blink_auth`/`blink_models`/
`blink_engine` localement, juste avant `runtime.bootstrap()`, au lieu qu'un
import de tête paie ce coût pour tout le monde. C'est le même principe que
`deleguer()` applique déjà pour merge_daily/serve/watch/maj/autostart."""

from __future__ import annotations  # Python 3.8 (build Windows 7) : les annotations "X | None" ne s'évaluent qu'à l'écriture des chaînes, jamais à l'exécution.

import argparse
import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path

import runtime

import blink_registre


def parse_args() -> argparse.Namespace:
    programme = Path(sys.argv[0]).stem or "blink2video"
    version = runtime.version_affichee()
    # Verbe.fr/.en existaient déjà mais seul .fr était lu ici : l'aide
    # principale restait en français quelle que soit la langue (issue #16).
    langue = runtime.lire_langue()
    parser = argparse.ArgumentParser(
        prog=programme,
        # Les verbes vont dans la description, pas dans un groupe d'arguments :
        # les déclarer à argparse en ferait de faux positionnels, qui
        # pollueraient la ligne d'usage et fausseraient l'analyse.
        description=(
            f"blink2video {version}\n\n"
            + msg("aide_description") + "\n\n" + msg("aide_verbes_titre") + "\n"
            + "".join(f"  {nom:11} {getattr(verbe, langue)}\n"
                      for nom, verbe in runtime.VERBES.items())
            + "\n  " + msg("aide_verbe_help")
        ),
        # Les exemples suivent l'ordre dans lequel on rencontre les verbes :
        # se connecter, regarder ce qu'il y a, récupérer, assembler, visionner,
        # puis automatiser. C'est un parcours, pas un catalogue.
        epilog=msg("aide_premiers_pas") + "\n" + "\n".join(
            f"  {programme} {commande:<20} {msg(cle)}"
            for commande, cle in (
                ("login", "aide_pas_login"),
                ("list", "aide_pas_list"),
                ("download", "aide_pas_download"),
                ("merge", "aide_pas_merge"),
                ("serve", "aide_pas_serve"),
                ("autostart on", "aide_pas_autostart"),
            )
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version",
                        version=f"blink2video {version}")
    parser.add_argument(
        "command",
        nargs="?",
        choices=tuple(runtime.VERBES),
        # Pas de commande par défaut : sans argument, on affiche l'aide plutôt
        # que d'ouvrir une connexion au compte Blink. Une commande lancée sans
        # rien ne doit pas partir sur le réseau à l'insu de celui qui la tape.
        default=None,
        # L'aide détaillée de chaque verbe est imprimée sous l'aide standard,
        # en une seule liste : séparer les verbes traités ici de ceux qui sont
        # délégués n'apprend rien à l'utilisateur et laisse croire que les
        # premiers n'existent pas.
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--hub", help=msg("aide_hub"))
    parser.add_argument("--camera", help=msg("aide_camera"))
    parser.add_argument(
        "--since",
        type=runtime.jours_non_negatifs,
        metavar=msg("aide_metavar_jours"),
        help=msg("aide_since"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=blink_registre.OUTPUT,
        help=msg("aide_output", defaut=blink_registre.OUTPUT),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=msg("aide_overwrite"),
    )
    parser.add_argument(
        "--from", dest="source", choices=("usb", "cloud", "all"), default="all",
        help=msg("aide_from"),
    )
    # Une boucle propre au verbe : le cloud se sonde à la minute sans rien
    # réveiller, là où le manifeste local mobilise le module et se contente de dix
    # minutes. Deux cadences valent mieux qu'un compromis unique.
    runtime.ajouter_boucle(parser)
    # Planificateur interne utilisé par « start » : un même processus fait le
    # premier inventaire USB+cloud (un seul total), puis respecte les cadences
    # différentes des deux sources. Ces options restent cachées : --loop est
    # l'interface ordinaire pour une commande tapée à la main.
    parser.add_argument("--usb-loop", type=runtime.cadence_positive,
                        default=None, help=argparse.SUPPRESS)
    parser.add_argument("--cloud-loop", type=runtime.cadence_positive,
                        default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.command is None:
        # Sans commande, l'aide plutôt qu'une connexion au compte : une
        # commande tapée sans argument ne doit pas partir sur le réseau.
        parser.print_help()
        raise SystemExit(0)
    cadences_sources = (args.usb_loop is not None, args.cloud_loop is not None)
    if any(cadences_sources) and not all(cadences_sources):
        parser.error(msg("erreur_cadences_ensemble"))
    if all(cadences_sources) and (
            args.command != "download" or args.source != "all" or args.loop is not None):
        parser.error(msg("erreur_cadences_exigent"))
    return args


LIBELLES = {
    "fr": {
        "aide_description":
            "Gestion des caméras Blink depuis un ordinateur : direct, "
            "armement, archive horodatée.",
        "aide_verbes_titre": "Verbes :",
        "aide_verbe_help": "<verbe> --help donne les options de chacun.",
        "aide_premiers_pas": "Premiers pas :",
        "aide_pas_login": "se connecter une fois",
        "aide_pas_list": "voir ce que contient le module",
        "aide_pas_download": "récupérer les clips",
        "aide_pas_merge": "assembler les vidéos",
        "aide_pas_serve": "ouvrir l'interface",
        "aide_pas_autostart": "surveiller à chaque session",
        "aide_hub": "nom du Sync Module à utiliser",
        "aide_camera": "ne garder que cette caméra",
        "aide_since": "ne garder que les clips des N derniers jours",
        "aide_metavar_jours": "JOURS",
        "aide_output": "dossier de destination (défaut : {defaut})",
        "aide_overwrite": "forcer le retéléchargement des clips visibles, même déjà acquis",
        "aide_from":
            "où chercher les clips : « usb » la clé du module, « cloud » celui "
            "de l'abonnement, « all » les deux (défaut)",
        "erreur_cadences_ensemble": "--usb-loop et --cloud-loop s'emploient ensemble",
        "erreur_cadences_exigent":
            "les cadences USB/cloud exigent « download --from all » sans --loop",
        "aide_desc_open": "Ouvre l'interface dans le navigateur, et dit si personne n'écoute.",
        "aide_port_open": "port de l'interface (défaut : le port configuré)",
        "aide_desc_stop":
            "Arrête les instances en cours, y compris celle du démarrage automatique.",
        "aide_desc_restart": "Arrête l'instance en cours, puis relance « start » à neuf.",
        "aide_sans_relance": "s'arrêter sans relancer ensuite",
        "echec_connexion": "\nÉchec de la connexion Blink.",
        "connexion_reussie": "\nConnexion Blink réussie.",
        "session_sauvegardee": "Session sauvegardée dans : {chemin}",
        "sync_modules_titre": "\n=== SYNC MODULES ===",
        "aucun_sync_module": "Aucun Sync Module trouvé sur ce compte.",
        "sync_module_ligne": "- {nom} (ID {sync_id}, réseau {network_id})",
        "usb_exige_sync_module": "\nLe stockage local exige un Sync Module ; utilisez --from cloud.",
        "erreur_hub": "\nErreur : {erreur}",
        "personne_ecoute": "Personne n'écoute sur {adresse}.",
        "lancez_serve":
            "Lancez « blink2video serve », ou « blink2video autostart on » "
            "pour que l'interface démarre avec la session.",
        "ouverture_adresse": "Ouverture de {adresse}",
        "rien_ne_tourne": "Rien ne tourne.",
        "arret_instance": "Arrêt de « {commande} » (PID {pid}, depuis {depuis})",
        "identite_pid_illisible": "  Identité du PID {pid} illisible : arrêt forcé ignoré.",
        "pid_reattribue":
            "  PID {pid} ne correspond plus à cette instance "
            "(numéro réattribué à un autre logiciel) : ignoré.",
        "toujours_en_vie": "Toujours en vie : {liste}",
        "arrete": "Arrêté.",
        "arret_redemarrage_en_cours": "Arrêt ou redémarrage déjà en cours ({erreur}).",
        "session_invalide_injoignable":
            "\nSession enregistrée invalide ou injoignable : {erreur}",
        "aucune_session_ouverture_connexion":
            "\nAucune session Blink valide. Ouverture de la page de connexion...",
        "premiere_utilisation":
            "\nPremière utilisation : vérifiez les réglages avant le téléchargement...",
        "etape_configuration": "la fin de la configuration",
        "etape_connexion": "la connexion",
        "interface_arretee_avant":
            "L'interface s'est arrêtée avant {etape} (code {code}). Abandon.",
        "interface_ne_repond_pas": "L'interface ne répond pas sur {adresse}. Abandon.",
        "page_configuration": "configuration",
        "page_connexion": "connexion",
        "page_prete": "Page de {page} prête sur {adresse} (navigateur non ouvert).",
        "navigateur_indisponible":
            "Navigateur indisponible : ouvrez {adresse} vous-même, "
            "ou utilisez « blink2video login » dans un terminal.",
        "compte_accessible": "compte accessible, {n} caméra(s)",
        "cloud_uniquement": " (compte sans Sync Module, cloud uniquement)",
        "connexion_reussie_resume": "\nConnexion réussie : {resume}.",
        "reglages_initiaux_enregistres":
            "\nRéglages initiaux enregistrés. Démarrage des téléchargements...",
        "delai_configuration_depasse":
            "\nDélai de configuration dépassé. Abandon, aucun téléchargement lancé.",
        "delai_connexion_depasse":
            "\nDélai de connexion dépassé. Abandon, rien n'est laissé actif.",
        "start_aide_usage": "usage : blink2video start [options de serve]",
        "start_aide_intro": "Lance la configuration recommandée :",
        "start_aide_options": "Les options données ici vont à l'interface, --port par exemple.",
        "start_aide_stop": "« blink2video stop » arrête l'ensemble.",
        "demarrage_deja_en_cours": "Démarrage déjà en cours ailleurs, ouverture de l'interface...",
        "stop_seul": "« stop » s'emploie seul : il arrête ce qui tourne déjà.",
        "impossible_demarrer_pendant_arret": "Impossible de démarrer pendant un arrêt ({erreur}).",
        "lance": "Lancé : {commande}",
        "etape_verbe": "Étape : {commande}",
        "arrete_verbe_code": "Arrêté : {verbe} (code {code})",
        "arrete_verbe_normal": "Arrêté : {verbe} (fin normale)",
        "interruption_clavier": "\nArrêt.",
        "option_avant_verbe":
            "« {option} » précède le premier verbe : les options "
            "suivent le verbe auquel elles s'appliquent.",
        "liste_verbes": "Verbes : {liste}",
        "connexion_annulee": "\nConnexion annulée.",
    },
    "en": {
        "aide_description":
            "Manage Blink cameras from a computer: live view, arming, "
            "timestamped archive.",
        "aide_verbes_titre": "Verbs:",
        "aide_verbe_help": "<verb> --help shows the options of each.",
        "aide_premiers_pas": "Getting started:",
        "aide_pas_login": "sign in once",
        "aide_pas_list": "see what the Sync Module holds",
        "aide_pas_download": "fetch the clips",
        "aide_pas_merge": "assemble the videos",
        "aide_pas_serve": "open the interface",
        "aide_pas_autostart": "monitor at every login",
        "aide_hub": "name of the Sync Module to use",
        "aide_camera": "keep only this camera",
        "aide_since": "keep only clips from the last N days",
        "aide_metavar_jours": "DAYS",
        "aide_output": "destination folder (default: {defaut})",
        "aide_overwrite": "force re-downloading visible clips, even ones already fetched",
        "aide_from":
            "where to look for clips: « usb » the Sync Module's USB drive, "
            "« cloud » the subscription's cloud, « all » both (default)",
        "erreur_cadences_ensemble": "--usb-loop and --cloud-loop go together",
        "erreur_cadences_exigent":
            "USB/cloud intervals require « download --from all » without --loop",
        "aide_desc_open": "Opens the interface in the browser, and says if nothing is listening.",
        "aide_port_open": "interface port (default: the configured port)",
        "aide_desc_stop": "Stops the running instances, including the autostart one.",
        "aide_desc_restart": "Stops the running instance, then relaunches « start » fresh.",
        "aide_sans_relance": "stop without relaunching afterwards",
        "echec_connexion": "\nBlink sign-in failed.",
        "connexion_reussie": "\nSigned in to Blink.",
        "session_sauvegardee": "Session saved to: {chemin}",
        "sync_modules_titre": "\n=== SYNC MODULES ===",
        "aucun_sync_module": "No Sync Module found on this account.",
        "sync_module_ligne": "- {nom} (ID {sync_id}, network {network_id})",
        "usb_exige_sync_module": "\nLocal storage needs a Sync Module; use --from cloud.",
        "erreur_hub": "\nError: {erreur}",
        "personne_ecoute": "Nobody is listening on {adresse}.",
        "lancez_serve":
            "Run « blink2video serve », or « blink2video autostart on » "
            "for the interface to start with the session.",
        "ouverture_adresse": "Opening {adresse}",
        "rien_ne_tourne": "Nothing is running.",
        "arret_instance": "Stopping « {commande} » (PID {pid}, since {depuis})",
        "identite_pid_illisible": "  PID {pid} identity unreadable: forced stop skipped.",
        "pid_reattribue":
            "  PID {pid} no longer matches this instance "
            "(number reassigned to another program): skipped.",
        "toujours_en_vie": "Still alive: {liste}",
        "arrete": "Stopped.",
        "arret_redemarrage_en_cours": "Stop or restart already in progress ({erreur}).",
        "session_invalide_injoignable":
            "\nSaved session invalid or unreachable: {erreur}",
        "aucune_session_ouverture_connexion":
            "\nNo valid Blink session. Opening the sign-in page...",
        "premiere_utilisation":
            "\nFirst use: check the settings before downloading...",
        "etape_configuration": "the end of setup",
        "etape_connexion": "sign-in",
        "interface_arretee_avant":
            "The interface stopped before {etape} (code {code}). Aborting.",
        "interface_ne_repond_pas": "The interface is not responding on {adresse}. Aborting.",
        "page_configuration": "setup",
        "page_connexion": "sign-in",
        "page_prete": "{page} page ready on {adresse} (browser not opened).",
        "navigateur_indisponible":
            "Browser unavailable: open {adresse} yourself, "
            "or use « blink2video login » in a terminal.",
        "compte_accessible": "account reachable, {n} camera(s)",
        "cloud_uniquement": " (account with no Sync Module, cloud only)",
        "connexion_reussie_resume": "\nSigned in: {resume}.",
        "reglages_initiaux_enregistres":
            "\nInitial settings saved. Starting downloads...",
        "delai_configuration_depasse":
            "\nSetup timed out. Aborting, no download started.",
        "delai_connexion_depasse":
            "\nSign-in timed out. Aborting, nothing left running.",
        "start_aide_usage": "usage: blink2video start [serve options]",
        "start_aide_intro": "Launches the recommended setup:",
        "start_aide_options": "Options given here go to the interface, --port for example.",
        "start_aide_stop": "« blink2video stop » stops everything.",
        "demarrage_deja_en_cours": "Already starting elsewhere, opening the interface...",
        "stop_seul": "« stop » is used alone: it stops what's already running.",
        "impossible_demarrer_pendant_arret": "Cannot start while stopping ({erreur}).",
        "lance": "Started: {commande}",
        "etape_verbe": "Step: {commande}",
        "arrete_verbe_code": "Stopped: {verbe} (code {code})",
        "arrete_verbe_normal": "Stopped: {verbe} (normal exit)",
        "interruption_clavier": "\nStopping.",
        "option_avant_verbe":
            "« {option} » comes before the first verb: options "
            "follow the verb they apply to.",
        "liste_verbes": "Verbs: {liste}",
        "connexion_annulee": "\nLogin cancelled.",
    },
}


def msg(cle: str, **valeurs) -> str:
    return runtime.traduire(LIBELLES, cle, **valeurs)


async def main(args: argparse.Namespace) -> int:
    # Lazy : seuls login/list/download/start (via ce chemin) paient le coût
    # d'aiohttp et de blinkpy (O-06/8.7/8.8).
    runtime.bootstrap()
    import blink_auth
    import blink_engine
    import blink_models

    async with blink_auth.session_http_temporaire() as session:
        blink = await blink_auth.connect(session)
        if blink is None:
            print(msg("echec_connexion"))
            return 1

        print(msg("connexion_reussie"))
        print(msg("session_sauvegardee", chemin=blink_auth.CONFIG.resolve()))
        print(msg("sync_modules_titre"))
        # Le homescreen moderne peut annoncer un véritable module de stockage
        # que les anciens endpoints encore utilisés par blinkpy 0.25.9 n'ont
        # pas placé dans blink.sync (notamment avec un XR). La même découverte
        # que le téléchargeur doit donc précéder l'affichage et le garde-fou USB.
        modules_disponibles = blink_models.select_sync_modules(blink, None)
        if not modules_disponibles:
            print(msg("aucun_sync_module"))
        for name, sync in modules_disponibles:
            print(msg("sync_module_ligne", nom=name, sync_id=sync.sync_id,
                      network_id=sync.network_id))

        if args.command == "login":
            return 0

        if args.source == "usb" and not modules_disponibles:
            print(msg("usb_exige_sync_module"))
            return 1

        try:
            modules = (
                [] if args.source == "cloud"
                else (modules_disponibles if not args.hub
                      else blink_models.select_sync_modules(blink, args.hub))
            )
        except ValueError as error:
            print(msg("erreur_hub", erreur=error))
            return 2

        return await blink_engine.boucler(blink, args, modules)


# Point d'entrée unique. Les autres programmes gardent leur propre fichier et
# leur propre analyse d'arguments : on ne fait que les appeler, sans rien
# déplacer. Fusionner les quatre en un seul fichier donnerait un script de
# quatre mille lignes, moins lisible et impossible à éprouver par morceaux.
#
# C'est la forme des commandes à verbe, celle de git ou de docker : un nom à
# retenir, un verbe pour l'action. Chaque verbe reçoit tels quels les arguments
# qui le suivent, donc « blink2video.py review --port 8899 » revient exactement à
# « blink2video serve --port 8899 ».
# Les verbes, leur programme et leur description vivent dans runtime.VERBES :
# une seule table, lue ici pour l'aide et la délégation, par self_command pour
# la relance, et par docs.py pour les README.
DELEGUES = runtime.DELEGUES


def deleguer(verbe: str, arguments: list) -> int:
    """Passe la main au programme d'un verbe, dans le même processus.

    L'import est fait ici et pas en tête de fichier : ces modules importent
    eux-mêmes blink2video.py, et surtout ils tirent ffmpeg ou aiohttp derrière eux.
    Une simple demande de manifeste n'a pas à payer ce chargement."""
    import importlib

    module = importlib.import_module(DELEGUES[verbe])
    sys.argv = [f"{DELEGUES[verbe]}.py", *arguments]
    return int(module.main() or 0)


def ouvrir(arguments: list = ()) -> int:
    """Ouvre l'interface dans le navigateur, et dit si personne n'écoute.

    L'adresse est évidente pour qui la connaît ; elle ne l'est pas pour qui
    installe l'outil. Un verbe se trouve dans « --help », un port se retient
    mal."""
    import socket
    import webbrowser

    parseur = argparse.ArgumentParser(
        prog="blink2video open",
        description=msg("aide_desc_open"),
    )
    # Le port configuré, pas 8765 en dur : sans ça, « open » sans argument
    # ouvrait toujours 8765 même après un changement de port dans les
    # réglages (revue du 27/08, bug 5).
    parseur.add_argument("--port", type=runtime.port_valide,
                         default=runtime.lire_reglages()["port"],
                         help=msg("aide_port_open"))
    options = parseur.parse_args(list(arguments))
    adresse = f"http://127.0.0.1:{options.port}/"

    with socket.socket() as prise:
        prise.settimeout(2)
        if prise.connect_ex(("127.0.0.1", options.port)) != 0:
            print(msg("personne_ecoute", adresse=adresse))
            print(msg("lancez_serve"))
            return 1

    print(msg("ouverture_adresse", adresse=adresse))
    webbrowser.open(adresse)
    return 0


def _arreter_instances() -> int:
    """Corps de l'arrêt, appelé sous le verrou global de contrôle."""
    instances = runtime.lire_instances()
    if not instances:
        runtime.effacer_arret_demande()
        print(msg("rien_ne_tourne"))
        return 0

    # Drapeau coopératif posé avant tout kill (revue du 27/08) : serve,
    # watch, download et merge le relisent (repeter()/shutdown()) et
    # sortent d'eux-mêmes entre deux tours plutôt que d'être tués en plein
    # milieu. Le kill ci-dessous reste le repli pour ce qui n'a pas eu le
    # temps de sortir seul dans le délai de grâce.
    runtime.demander_arret()

    restants = []
    for fiche in instances:
        commande = " ".join(" ".join(groupe) for groupe in fiche.get("verbes") or [])
        print(msg("arret_instance", commande=commande or "blink2video",
                  pid=fiche['pid'], depuis=fiche.get('depuis', '?')))
        # Un numéro de processus fini par être réattribué à un logiciel sans
        # aucun rapport ; le confondre avec l'instance qu'on croit suivre a
        # déjà fait « arrêter » un service tiers et une messagerie sur la
        # machine d'un utilisateur. On vérifie l'identité avant de tuer quoi
        # que ce soit, jamais seulement l'existence du PID.
        identites = fiche.get("identites") or {}
        if not isinstance(identites, dict):
            identites = {}

        def etat_processus(pid: int, marqueurs=None):
            """True=même processus, False=terminé/recyclé, None=indécidable."""
            pid = int(pid)
            attendue = identites.get(str(pid))
            if not runtime.processus_vivant(pid):
                return False
            if attendue is None:
                # Fiche de l'ancienne version : repli temporaire sur la ligne
                # de commande (compatible PowerShell 2 dans runtime.py).
                return runtime.processus_correspond(pid, marqueurs)
            actuelle = runtime.identite_processus(pid)
            if actuelle is None:
                return None
            return actuelle == attendue

        membres = [int(pid) for pid in
                   [fiche["pid"], *(fiche.get("enfants") or [])]]
        travailleurs = [int(pid) for pid in fiche.get("travailleurs") or []]
        # Un ffmpeg de fusion (merge_daily.run_ffmpeg_batch/concat_copy) ne
        # comprend pas le drapeau (processus tiers, aucune prise dessus) :
        # tué directement, sans délai de grâce. Jamais droit au taskkill /T
        # sur son propre PID (protection du navigateur, voir
        # arreter_processus) : sans ça il resterait orphelin, tournant
        # jusqu'à sa fin après « stop ». Empreinte dédiée : sa ligne de
        # commande ne porte jamais « blink2video ».
        for pid_ffmpeg in travailleurs:
            etat = etat_processus(pid_ffmpeg, ["ffmpeg"])
            if etat is True:
                runtime.arreter_processus(pid_ffmpeg, avec_descendance=True)
            elif etat is None:
                print(msg("identite_pid_illisible", pid=pid_ffmpeg))

        # Délai de grâce pour les membres Python (serve/watch/download/
        # merge) : le drapeau posé plus haut leur laisse la chance de sortir
        # d'eux-mêmes avant qu'on ne force. processus_vivant() seul ici, pas
        # processus_correspond() : constaté en conditions réelles (revue du
        # 27/08), la vérification d'identité complète (tasklist + PowerShell
        # par membre) coûtait assez cher, répétée chaque seconde, pour faire
        # dépasser 15 s de largement plus de moitié sur 2 membres seulement -
        # bien pire avec les 5 verbes d'une vraie composition. La
        # vérification d'identité complète reste faite une seule fois,
        # juste après, avant le kill effectif ; un pid réattribué pendant ce
        # délai de grâce (fenêtre de quelques secondes, cas rarissime) fait
        # au pire attendre le délai complet pour rien, jamais tuer à tort.
        # monotonic, pas time.time() : une correction NTP/DST pendant
        # l'attente ne doit pas raccourcir ni rallonger ce délai de grâce
        # de 15 s (même correctif que verrou() dans runtime.py, trouvé au
        # même audit).
        limite = time.monotonic() + 15
        pids_python = list(membres)
        while time.monotonic() < limite and any(
                runtime.processus_vivant(int(m)) for m in pids_python):
            time.sleep(1)

        for membre in membres:
            if not runtime.processus_vivant(int(membre)):
                continue
            etat = etat_processus(membre)
            if etat is None:
                print(msg("identite_pid_illisible", pid=membre))
                continue
            if not etat:
                print(msg("pid_reattribue", pid=membre))
                continue
            runtime.arreter_processus(int(membre),
                                       avec_descendance=(membre != fiche["pid"]))

        # I-14 : la fiche n'est retirée que si tout son monde est bien mort.
        # Elle restait effacée inconditionnellement ici, avant même de savoir
        # si l'arrêt avait réussi ; un survivant devenait alors introuvable au
        # « stop » suivant, sa seule piste ayant disparu avec la fiche. Un
        # membre dont le PID existe mais ne correspond plus à cette instance
        # (ci-dessus) ne compte pas comme un survivant : c'est notre
        # processus à nous qui est bien mort, seul son numéro a été repris.
        survivants = []
        for membre in [*membres, *travailleurs]:
            etat = etat_processus(membre, ["ffmpeg"] if membre in travailleurs else None)
            if etat is True or etat is None:
                survivants.append(str(membre))
        if survivants:
            restants.extend(survivants)
            continue
        Path(fiche["fiche"]).unlink(missing_ok=True)
    if restants:
        # Conserver la demande d'arrêt : un processus lent ou momentanément
        # impossible à tuer pourra encore la voir et sortir proprement. La
        # retirer ici le faisait repartir comme si rien ne s'était passé.
        print(msg("toujours_en_vie", liste=", ".join(restants)))
        return 1
    runtime.effacer_arret_demande()
    print(msg("arrete"))
    return 0


def arreter(arguments: list = ()) -> int:
    """Arrête les instances en cours, y compris celle du démarrage automatique.

    Une instance lancée sans console ne peut pas recevoir de Ctrl+C, et la tuer
    par son seul numéro laissait ses verbes derrière elle : « watch » continuait
    de tourner, orphelin, en tenant le module de synchronisation. La fiche
    déposée au démarrage donne le processus à interrompre, et le système donne
    sa descendance."""
    # Les options passent par argparse comme pour les autres verbes, même s'il
    # n'en a aucune : sans cela « stop --help » arrêtait l'instance au lieu de
    # s'expliquer, ce que la suite de tests faisait à chaque passage, sur
    # l'instance réelle de la machine.
    argparse.ArgumentParser(
        prog="blink2video stop",
        description=msg("aide_desc_stop"),
    ).parse_args(list(arguments))
    try:
        with runtime.verrou_controle("stop"):
            return _arreter_instances()
    except runtime.BusyError as erreur:
        print(msg("arret_redemarrage_en_cours", erreur=erreur))
        return 1


def redemarrer(arguments: list = ()) -> int:
    """Arrête l'instance en cours, puis relance « start » à neuf.

    À la différence de « update », qui restaure exactement la composition
    d'avant (mêmes --loop, relus dans la fiche), ce verbe relance « start »
    en clair : les cadences USB/cloud et tout ce qui a changé dans le
    fichier de réglages depuis sont donc repris, pas rejoués. Sert le
    panneau de réglages de la page web : --sans-relance pour son bouton
    Stop, sans option pour son bouton Appliquer.

    --finaliser est un détail d'implémentation, jamais tapé à la main :
    appelé depuis « serve », ce verbe est l'enfant du processus que « stop »
    va abattre, branche entière comprise sous Windows (taskkill /T). Comme
    maj.installer/finaliser, un premier temps se contente de lancer le
    second puis rend la main aussitôt : le temps que « stop » commence à
    chercher son arbre, ce premier temps a déjà disparu, et le second, déjà
    détaché, lui échappe."""
    parser = argparse.ArgumentParser(
        prog="blink2video restart",
        description=msg("aide_desc_restart"),
    )
    parser.add_argument("--finaliser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--delai", type=float, default=0.0, help=argparse.SUPPRESS)
    parser.add_argument("--sans-relance", action="store_true",
                        help=msg("aide_sans_relance"))
    args = parser.parse_args(list(arguments))
    installe = runtime.app_dir()

    if not args.finaliser:
        suite = ["--finaliser"] + (["--sans-relance"] if args.sans_relance else [])
        if args.delai > 0:
            suite.extend(["--delai", str(args.delai)])
        runtime.demarrer(runtime.self_command("restart", *suite),
                         cwd=str(installe), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=(sys.platform != "win32"))
        return 0

    try:
        # Un seul finaliseur à la fois. attente=0 est volontaire : deux clics
        # rapprochés ne doivent pas mettre le second en file puis lui faire
        # arrêter la composition toute neuve lancée par le premier.
        with runtime.verrou_controle("restart"):
            if args.delai > 0:
                time.sleep(args.delai)
            code = _arreter_instances()
            if code:
                # Ne jamais empiler une nouvelle composition sur une instance
                # que l'arrêt n'a pas réussi à terminer.
                return code
            # Les fichiers restent tenus quelques instants après la mort du
            # processus, le temps que Windows referme ses poignées.
            instances = []
            for _ in range(20):
                instances = runtime.lire_instances()
                if not instances:
                    break
                time.sleep(1)
            if instances:
                return 1
            if not args.sans_relance:
                runtime.demarrer(runtime.self_command("start"), cwd=str(installe),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 start_new_session=(sys.platform != "win32"))
            return 0
    except runtime.BusyError:
        return 1


def _port_demande(arguments: list) -> int | None:
    """Port explicitement demandé dans ces arguments, ou None si absent.

    Extraction minimale et tolérante aux autres options (--timezone, etc.) :
    sert à ce que le raccourci « déjà démarré » de start regarde le même
    port que celui qui sera réellement utilisé pour le serve relancé plus
    loin, plutôt que de toujours se fier au port enregistré même quand un
    --port explicite l'écrase (revue du 27/08, bug 5)."""
    mini = argparse.ArgumentParser(add_help=False)
    mini.add_argument("--port", type=runtime.port_valide, default=None)
    options, _ = mini.parse_known_args(list(arguments))
    return options.port


def _port_ouvert(port: int) -> bool:
    """Une fonction à elle seule pour rester mockable sans toucher au module
    socket global, dont asyncio se sert aussi pour sa propre boucle."""
    import socket

    with socket.socket() as prise:
        prise.settimeout(1)
        return prise.connect_ex(("127.0.0.1", port)) == 0


MARQUEUR_RACCOURCI = ".blink_raccourci_cree"


def _proposer_raccourci_bureau() -> None:
    """Pose le raccourci de bureau au tout premier « start » réussi, jamais
    ensuite : le marqueur empêche de le recréer si l'utilisateur l'a
    supprimé volontairement par la suite. Best-effort : un échec ici (pas
    d'environnement de bureau, permission refusée...) ne doit jamais faire
    échouer le démarrage lui-même."""
    marqueur = runtime.app_dir() / MARQUEUR_RACCOURCI
    if marqueur.exists():
        return
    try:
        marqueur.touch()
    except OSError:
        return
    try:
        import raccourci_bureau
        raccourci_bureau.creer()
    except Exception:
        pass


def accueillir(etat: dict, supplement: list, delai: float = 600.0,
               configuration_initiale: bool = False) -> int:
    """Onboarding : connexion Blink puis, sur une installation neuve,
    validation des réglages avant de laisser « start » continuer.

    Le serveur ouvert ici est temporaire : une fois la connexion réussie, il
    est arrêté et « start » relance la composition complète, serve compris,
    pour ne pas avoir à faire rejoindre un processus déjà en cours à la
    supervision normale. Le coût est un bref redémarrage de l'interface,
    invisible à l'usage sauf onglet déjà ouvert à ce moment précis.

    Deux variables d'environnement, lues nulle part ailleurs que dans les
    tests et jamais documentées côté utilisateur : BLINK_ONBOARDING_TIMEOUT
    raccourcit l'attente (le délai réel d'un humain qui lit un e-mail de code
    2FA n'a pas sa place dans une suite automatisée), BLINK_NO_BROWSER
    supprime l'ouverture réelle d'un navigateur, que la suite de tests ne
    doit jamais déclencher (section 12.5).

    En mode configuration initiale, seul ``serve --initial-setup`` tourne :
    aucun worker download/watch/merge n'existe encore. Le parent attend le
    marqueur écrit par /api/reglages, arrête ce serveur temporaire, puis lance
    la composition complète depuis la racine de stockage désormais choisie."""
    import os
    import webbrowser

    delai = float(os.environ.get("BLINK_ONBOARDING_TIMEOUT", delai))
    # Même bloc fixe que la composition complète (runtime.standard() : port,
    # fuseau et hôte de confiance enregistrés), suivi du supplément tapé à la
    # main, qui l'emporte (argparse retient la dernière occurrence). Sans lui,
    # le serveur temporaire partait sur le 8765 codé en dur de serve.py alors
    # que l'utilisateur avait choisi un autre port dans les réglages - souvent
    # précisément parce que 8765 était pris : session expirée + port
    # personnalisé = connexion impossible (audit du 2026-09-24).
    options_serveur = [*runtime.standard()[1:runtime.LONGUEUR_BLOC_SERVE],
                       *supplement]
    # Le parseur minimal commun comprend les deux formes argparse,
    # ``--port 9000`` et ``--port=9000``. Une seconde extraction manuelle
    # vivait auparavant ici : la forme avec ``=`` lançait bien serve sur le
    # port demandé, mais l'onboarding sondait et ouvrait encore 8765.
    port = _port_demande(options_serveur)

    connexion_requise = not bool(etat.get("authenticated"))
    if etat.get("error"):
        print(msg("session_invalide_injoignable", erreur=etat['error']))
    if connexion_requise:
        print(msg("aucune_session_ouverture_connexion"))
    elif configuration_initiale:
        print(msg("premiere_utilisation"))

    if configuration_initiale:
        options_serveur.append("--initial-setup")
    processus = runtime.demarrer(
        runtime.self_command("serve", *options_serveur),
        cwd=str(runtime.app_dir()), creationflags=runtime.flags_enfant(),
        start_new_session=(os.name != "nt"),
    )
    parametres = []
    if connexion_requise:
        parametres.append("login=1")
    if configuration_initiale:
        parametres.append("setup=1")
    adresse = f"http://127.0.0.1:{port}/"
    if parametres:
        adresse += "?" + "&".join(parametres)

    def echec_processus() -> int:
        etape = msg("etape_configuration") if configuration_initiale else msg("etape_connexion")
        print(msg("interface_arretee_avant", etape=etape, code=processus.returncode))
        return processus.returncode or 1

    # Propriétaire jusqu'au bout (5.16) : sur toute issue, ce serveur
    # temporaire est arrêté avant de rendre la main. Sur succès, « start »
    # relance juste après la composition complète, serve compris, plutôt que
    # de faire rejoindre ce processus déjà en cours à la supervision normale.
    try:
        limite = time.monotonic() + delai
        pret = False
        while time.monotonic() < limite:
            if processus.poll() is not None:
                return echec_processus()
            if _port_ouvert(port):
                pret = True
                break
            time.sleep(0.5)
        if not pret:
            print(msg("interface_ne_repond_pas", adresse=adresse))
            return 1

        if os.environ.get("BLINK_NO_BROWSER") == "1":
            page = msg("page_configuration") if configuration_initiale else msg("page_connexion")
            print(msg("page_prete", page=page, adresse=adresse))
        else:
            print(msg("ouverture_adresse", adresse=adresse))
            if not webbrowser.open(adresse):
                print(msg("navigateur_indisponible", adresse=adresse))

        runtime.bootstrap()
        import blink_auth

        authentifie = not connexion_requise
        connexion_annoncee = authentifie
        while time.monotonic() < limite:
            if processus.poll() is not None:
                return echec_processus()
            if not authentifie:
                resultat = asyncio.run(blink_auth.preflight())
                authentifie = bool(resultat["authenticated"])
            if authentifie and not connexion_annoncee:
                resume = msg("compte_accessible", n=resultat['cameras'])
                if resultat["cloud_only"]:
                    resume += msg("cloud_uniquement")
                print(msg("connexion_reussie_resume", resume=resume))
                connexion_annoncee = True
                if configuration_initiale:
                    # La saisie du code 2FA ne doit pas consommer le temps
                    # laissé pour lire et valider le panneau qui suit.
                    limite = time.monotonic() + delai
            if authentifie and not configuration_initiale:
                return 0
            if authentifie and runtime.configuration_initiale_effectuee():
                # /api/reglages pose le marqueur juste avant d'envoyer sa
                # réponse. Cette marge lui laisse le temps d'être transmise et
                # lue par un navigateur lent (notamment sous Windows 7) avant
                # que le finally ci-dessous coupe le serveur temporaire.
                time.sleep(0.75)
                print(msg("reglages_initiaux_enregistres"))
                return 0
            time.sleep(2)

        if authentifie and configuration_initiale:
            print(msg("delai_configuration_depasse"))
        else:
            print(msg("delai_connexion_depasse"))
        return 1
    finally:
        if processus.poll() is None:
            runtime.arreter_processus(processus.pid, avec_descendance=True)


def _groupe_persistant(groupe: list) -> bool:
    """Vrai si ce groupe reste actif jusqu'à un arrêt explicite.

    ``argparse`` accepte aussi bien ``--loop 10`` que ``--loop=10``. Garder
    cette reconnaissance à un seul endroit évite qu'une commande soit lancée
    en boucle tout en étant classée comme ponctuelle par le superviseur.
    """
    if not groupe:
        return False
    if groupe[0] == "serve":
        return True
    options = ("--loop", "--usb-loop", "--cloud-loop")
    return any(
        argument == option or argument.startswith(option + "=")
        for argument in groupe[1:]
        for option in options
    )


def executer(groupes: list) -> int:
    """Exécute les verbes cités, ensemble.

    Un seul verbe est traité dans ce processus, ce qui garde la sortie et le
    code de retour directs. Plusieurs sont lancés côte à côte et attendus : ils
    s'arrêtent ensemble, faute de quoi un Ctrl+C laisserait derrière lui des
    programmes sans personne pour les arrêter."""
    if len(groupes) == 1 and groupes[0][0] == "start":
        # L'aide doit s'afficher, pas déclencher la configuration : sans ce
        # traitement, « start --help » lançait les boucles et ne rendait jamais
        # la main, ce que la suite de tests a montré en se bloquant dessus.
        if {"-h", "--help"} & set(groupes[0][1:]):
            print(msg("start_aide_usage"))
            print()
            print(msg("start_aide_intro"))
            print()
            print("  blink2video " + " ".join(runtime.standard()))
            print()
            print(msg("start_aide_options"))
            print(msg("start_aide_stop"))
            return 0
        # « start » n'est pas un travail de plus : c'est le nom de la
        # composition recommandée, options comprises. Les options données après
        # lui s'ajoutent au premier verbe, « serve », d'où le --port qui marche.
        supplement = groupes[0][1:]
        # Calculé avant le verrou : le raccourci "déjà démarré" ci-dessous
        # comme le repli BusyError plus bas doivent tous deux regarder le
        # port explicitement demandé, pas seulement celui enregistré ; et le
        # repli en a besoin même quand l'acquisition du verrou échoue avant
        # d'avoir pu le calculer lui-même (revue du 27/08, bug 5).
        port_demande = _port_demande(supplement)
        # E-01 : la session enregistrée est réellement testée avant de lancer
        # quoi que ce soit de permanent, pas seulement vérifiée présente sur
        # disque (5.3, 5.4). Si elle manque ou n'est plus valide, l'interface
        # s'ouvre seule d'abord, avec la page de connexion prête, et les
        # boucles de fond n'apparaissent qu'après un succès confirmé (5.12,
        # 5.15, 5.16).
        #
        # Le verrou couvre tout ce bloc, jusqu'au lancement effectif des
        # processus : un raccourci bureau cliqué deux fois de suite (ou un
        # double-clic Explorer qui part deux fois) lance sinon deux « start »
        # en parallèle, tous deux passant la vérification du port avant que
        # le premier n'ait fini de démarrer. Le second cède la place plutôt
        # que de tenter un démarrage concurrent.
        try:
            with runtime.verrou("start", "start", attente=0):
                # Une instance écoute déjà : « start » se comporte alors
                # comme « open », sans rien relancer. Le même raccourci
                # (bureau ou autostart) sert donc aussi bien à démarrer
                # qu'à rouvrir l'interface déjà en place, sans avoir à
                # composer deux commandes séparées ni à ouvrir une seconde
                # fenêtre de console pour le vérifier.
                port = port_demande or runtime.lire_reglages()["port"]
                if _port_ouvert(port):
                    return ouvrir(["--port", str(port)])

                # Décidé AVANT la connexion : sur une installation neuve,
                # celle-ci crée blink_auth.json. Si on évaluait ensuite les
                # traces d'une ancienne installation, cette session toute
                # fraîche ferait sauter à tort le panneau initial. Le marqueur
                # « en attente » de runtime garde aussi cette décision après
                # une fermeture du navigateur au milieu du parcours.
                configuration_initiale = runtime.configuration_initiale_requise()
                runtime.bootstrap()
                import blink_auth
                etat = asyncio.run(blink_auth.preflight())
                if not etat["authenticated"] or configuration_initiale:
                    code = accueillir(
                        etat, supplement,
                        configuration_initiale=configuration_initiale)
                    if code != 0:
                        return code
                composition = runtime.standard()
                # Le bloc fixe (serve, --port, valeur, --timezone, valeur,
                # --trusted-host, valeur) précède toujours le supplément : un
                # « --port »/« --timezone »/« --trusted-host » tapé à la main
                # arrive donc après celui, déjà présent, de la configuration
                # enregistrée, et l'emporte (argparse retient la dernière
                # occurrence d'une option).
                n = runtime.LONGUEUR_BLOC_SERVE
                code = executer(runtime.decouper_verbes(
                    [*composition[:n], *supplement, *composition[n:]]))
                if code == 0:
                    _proposer_raccourci_bureau()
                return code
        except runtime.BusyError:
            print(msg("demarrage_deja_en_cours"))
            port = port_demande or runtime.lire_reglages()["port"]
            return ouvrir(["--port", str(port)])

    if len(groupes) == 1 and groupes[0][0] == "open":
        return ouvrir(groupes[0][1:])

    if len(groupes) == 1 and groupes[0][0] == "restart":
        return redemarrer(groupes[0][1:])

    if any(groupe[0] == "stop" for groupe in groupes):
        if len(groupes) > 1:
            print(msg("stop_seul"))
            return 2
        return arreter(groupes[0][1:])

    if len(groupes) == 1:
        verbe, *arguments = groupes[0]
        if verbe in DELEGUES:
            # « update » ne s'inscrit pas : une fiche sert à retrouver ce qu'il
            # faut arrêter, et la mise à jour est précisément ce qui arrête tout
            # le reste. Inscrite, elle se trouvait elle-même dans la liste et se
            # tuait au premier « stop », en silence et à mi-chemin.
            if verbe != "update":
                try:
                    with runtime.verrou_controle("launch", attente=10):
                        runtime.inscrire_instance(groupes)
                except runtime.BusyError as erreur:
                    print(msg("impossible_demarrer_pendant_arret", erreur=erreur))
                    return 1
            return deleguer(verbe, arguments)
        if _groupe_persistant(groupes[0]):
            # Un download lancé seul ne passe pas par le superviseur de la
            # branche multi-verbes. Sans fiche, ``stop`` concluait donc « rien
            # ne tourne » alors que la boucle restait active. Publier le parent
            # courant suffit, comme pour un verbe délégué lancé seul.
            try:
                with runtime.verrou_controle("launch", attente=10):
                    runtime.inscrire_instance(groupes)
            except runtime.BusyError as erreur:
                print(msg("impossible_demarrer_pendant_arret", erreur=erreur))
                return 1
        sys.argv = ["blink2video", verbe, *arguments]
        return asyncio.run(main(parse_args()))

    # Ce qui se termine s'enchaîne, ce qui ne se termine pas tourne à côté.
    # « serve » porte un --loop implicite : il ne rend jamais la main, comme
    # tout verbe à qui on demande de se répéter. Les autres font un passage et
    # s'arrêtent, donc les faire tourner en même temps n'aurait aucun sens :
    # l'assemblage démarrerait pendant que le téléchargement écrit encore.
    persistant = [g for g in groupes if _groupe_persistant(g)]
    ponctuels = [g for g in groupes if g not in persistant]

    lances = []
    try:
        # Section courte seulement : publier la fiche et ses enfants comme un
        # seul lancement face à stop/restart. Le verrou est libéré avant la
        # surveillance, qui peut durer des jours.
        with runtime.verrou_controle("launch", attente=10):
            runtime.inscrire_instance(groupes)
            try:
                for verbe, *arguments in persistant:
                    lances.append((verbe, runtime.demarrer(
                        runtime.self_command(verbe, *arguments), cwd=str(runtime.app_dir()),
                        creationflags=runtime.flags_enfant(),
                        # Sa propre session hors Windows : « stop » peut alors tuer son
                        # groupe, ffmpeg compris, sans emporter le terminal qui a lancé
                        # l'ensemble.
                        start_new_session=(sys.platform != "win32"))))
                    print(msg("lance", commande=f"{verbe} {' '.join(arguments)}".rstrip()))
            except Exception:
                # Un verbe de la composition a échoué à démarrer (ex. OSError) :
                # ceux déjà lancés avant lui ne doivent jamais devenir des
                # orphelins invisibles pour stop. inscrire_instance(groupes,
                # [...]) n'est atteint qu'après la boucle complète - sans ce
                # rollback, la fiche déjà écrite juste au-dessus (sans enfants)
                # ne référence que ce process superviseur, qui va lui-même se
                # terminer sous peu à cause de cette même exception, laissant
                # le premier enfant tourner indéfiniment sans que stop ne le
                # voie (trouvé en auditant ce fichier, vérifié par exécution
                # réelle : mock du 2e Popen en échec).
                for _, p in lances:
                    runtime.arreter_processus(p.pid, avec_descendance=True)
                raise
            runtime.inscrire_instance(groupes, [p.pid for _, p in lances])
    except runtime.BusyError as erreur:
        print(msg("impossible_demarrer_pendant_arret", erreur=erreur))
        return 1

    # Les passages uniques, l'un après l'autre, dans l'ordre où ils sont cités.
    pire_ponctuel = 0
    for verbe, *arguments in ponctuels:
        print(msg("etape_verbe", commande=f"{verbe} {' '.join(arguments)}".rstrip()))
        resultat = runtime.lancer(
            runtime.self_command(verbe, *arguments), cwd=str(runtime.app_dir()),
            stdin=subprocess.DEVNULL, check=False,
        )
        pire_ponctuel = max(pire_ponctuel, abs(resultat.returncode))
    if not lances:
        return pire_ponctuel

    # Surveillés ensemble plutôt qu'attendus l'un après l'autre : un verbe qui
    # meurt à la première seconde doit se voir tout de suite, et non à la fin
    # d'une boucle qui tournera des jours. Les autres continuent, l'interface
    # qui tombe n'étant pas une raison d'arrêter la surveillance.
    pire = 0
    annonces = set()

    def relever_arrets() -> None:
        nonlocal pire
        for rang, (verbe, processus) in enumerate(lances):
            code = processus.poll()
            if code is None or rang in annonces:
                continue
            annonces.add(rang)
            pire = max(pire, abs(code))
            print(msg("arrete_verbe_code", verbe=verbe, code=code) if code
                  else msg("arrete_verbe_normal", verbe=verbe))

    def surveiller(sur_fin=None) -> None:
        # Purge des fiches d'instances mortes sans passage par « stop » (kill
        # externe, plantage, coupure de courant) : lire_instances() les
        # nettoie déjà comme effet de bord de sa lecture, mais rien ne
        # l'appelait plus tant que la session tourne ici sans redémarrer,
        # parfois des jours (constaté en réel, 2026-09-01 : fiches vieilles de
        # plusieurs jours toujours dans .blink_run). C'est ce tour, pas
        # relever_arrets() ni un_tour() d'un verbe, qui vit aussi longtemps
        # que la session : la responsabilité reste ici plutôt que dispersée
        # dans un verbe dont le rôle documenté est autre chose.
        MENAGE_INSTANCES_SECONDES = 600
        prochain_menage = time.monotonic() + MENAGE_INSTANCES_SECONDES
        while any(processus.poll() is None for _, processus in lances):
            relever_arrets()
            if time.monotonic() >= prochain_menage:
                runtime.lire_instances(journal=print)
                prochain_menage = time.monotonic() + MENAGE_INSTANCES_SECONDES
            time.sleep(1)
        # Le dernier processus (ou tous, s'ils ont fini avant notre premier
        # passage) rend la condition du while fausse. Il faut donc une collecte
        # terminale explicite, faute de quoi son code de sortie disparaît et un
        # échec isolé est annoncé comme un succès.
        relever_arrets()
        if sur_fin:
            sur_fin()

    # Repli sur le port configuré, pas 8765 en dur (même défaut incohérent
    # que le bug 5, revue du 27/08) : n'intervient que si serve tourne sans
    # --port explicite dans ses arguments, ce qui n'arrive plus via start
    # (composition toujours pourvue) mais reste possible pour un « serve »
    # tapé seul.
    port = runtime.lire_reglages()["port"]
    for verbe, *arguments in persistant:
        if verbe == "serve" and "--port" in arguments:
            port = int(arguments[arguments.index("--port") + 1])

    import tray

    def nettoyer_lances() -> None:
        # Arrêt coopératif d'abord (revue du 27/08) : le drapeau laisse
        # chaque verbe finir son tour en cours puis sortir de lui-même -
        # entre deux tours de repeter() (watch/download/merge) ou par
        # shutdown() entre deux requêtes (serve) - plutôt que d'être tué en
        # plein milieu. arreter_processus(avec_descendance=True) ne sert
        # plus qu'en repli, passé le délai de grâce, pour ce qui n'a pas eu
        # le temps de sortir seul. ffmpeg (que merge ne pilote pas de façon
        # coopérative, processus tiers) y passe presque toujours par ce
        # chemin, tué avec son parent via /T - .terminate() seul, avant ce
        # correctif, ne touchait que le processus immédiat, ne tuait jamais
        # ses enfants, et n'attendait pas la fin réelle (bug 6).
        #
        # Appelée directement par le menu Arrêter/Redémarrer de l'icône de
        # zone de notification (tray.executer, plus bas), pas seulement
        # depuis ce finally : passer par un « restart » détaché tout seul
        # laissait des processus vivants sur Windows 7 sans qu'on ait pu
        # établir pourquoi (icône disparue, rien derrière) - direct et
        # synchrone, ça ne dépend plus de ce second processus. Idempotente
        # (poll() déjà non-None ne refait rien) : l'appeler deux fois (ici et
        # depuis le finally) ne coûte qu'un aller-retour inutile si la
        # première a déjà tout nettoyé.
        runtime.demander_arret()
        limite = time.monotonic() + 15
        while time.monotonic() < limite and any(p.poll() is None for _, p in lances):
            time.sleep(0.2)
        for _, processus in lances:
            if processus.poll() is None:
                runtime.arreter_processus(processus.pid, avec_descendance=True)
        # taskkill est normalement synchrone, mais vérifier le résultat plutôt
        # que l'espérer. Si un processus résiste, garder le drapeau et la fiche
        # permet à un prochain stop de le retrouver au lieu de l'orpheliner.
        limite_forcee = time.monotonic() + 5
        while (time.monotonic() < limite_forcee
               and any(p.poll() is None for _, p in lances)):
            time.sleep(0.2)
        if not any(p.poll() is None for _, p in lances):
            runtime.effacer_arret_demande()

    try:
        # L'icône de zone de notification (Ouvrir/Redémarrer/Arrêter) exige
        # le thread principal sous macOS : la surveillance des verbes passe
        # alors sur un thread à part, qui referme l'icône si l'un d'eux
        # meurt de lui-même (crash), pour ne pas laisser une icône morte.
        if tray.disponible():
            fin = threading.Event()
            veilleur = threading.Thread(target=surveiller,
                                        kwargs={"sur_fin": fin.set}, daemon=True)
            veilleur.start()
            try:
                tray.executer(port, fin, nettoyer_lances)
            except Exception:
                # L'icône a échoué en cours de route (backend Linux qui se
                # dérobe, par exemple) : le thread de surveillance, lui,
                # continue, on se contente de l'attendre.
                pass
            veilleur.join()
        else:
            surveiller()
    except KeyboardInterrupt:
        print(msg("interruption_clavier"))
    finally:
        nettoyer_lances()
    return max(pire, pire_ponctuel)


def route(argv: list) -> int:
    """Dispatch pur à partir de sys.argv[1:] : une fonction ordinaire plutôt
    que du code coincé dans « if __name__ », pour rester testable sans passer
    par un sous-processus.

    E-01/5.1 : une liste vide emprunte exactement le même chemin que
    « start » (préflight compris), pour qu'une installation neuve n'ait plus
    à découvrir « login » ni « start ». --help et --version gardent leur
    sens : seule une liste réellement vide déclenche ce court-circuit,
    jamais une option isolée."""
    # --bootstrap= n'est déclaré par aucun parseur de verbe : retiré ici,
    # avant tout parse_args() en aval, sans quoi argparse le rejette comme
    # argument inconnu avant que bootstrap() ait pu le lire (revue du
    # 27/08, bug 4 - reproductible avec « download --bootstrap=none »).
    argv = runtime.extraire_mode_bootstrap(argv)
    if not argv:
        return executer(runtime.decouper_verbes(["start"]))
    # « autostart » vient nécessairement en tête : il n'exécute rien, il
    # ordonnance ce qui suit. Les autres verbes se citent dans n'importe
    # quel ordre, chacun suivi de ses options.
    if argv[0] == "autostart":
        return deleguer("autostart", argv[1:])
    if argv[0] in runtime.VERBES:
        return executer(runtime.decouper_verbes(argv))
    # Une option avant le premier verbe n'appartient à personne :
    # « blink2video --loop 5 merge » se lisait jusqu'ici comme une commande
    # racine qui boucle sur rien, et tournait indéfiniment sans rien faire.
    if argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version"):
        print(msg("option_avant_verbe", option=argv[0]))
        print(msg("liste_verbes", liste=", ".join(runtime.VERBES)))
        return 2
    sys.argv = ["blink2video", *argv]
    return asyncio.run(main(parse_args()))
