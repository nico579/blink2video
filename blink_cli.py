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
    parser = argparse.ArgumentParser(
        prog=programme,
        # Les verbes vont dans la description, pas dans un groupe d'arguments :
        # les déclarer à argparse en ferait de faux positionnels, qui
        # pollueraient la ligne d'usage et fausseraient l'analyse.
        description=(
            f"blink2video {version}\n\n"
            "Gestion des caméras Blink depuis un ordinateur : direct, "
            "armement, archive horodatée.\n\nVerbes :\n"
            + "".join(f"  {nom:11} {verbe.fr}\n"
                      for nom, verbe in runtime.VERBES.items())
            + "\n  <verbe> --help donne les options de chacun."
        ),
        # Les exemples suivent l'ordre dans lequel on rencontre les verbes :
        # se connecter, regarder ce qu'il y a, récupérer, assembler, visionner,
        # puis automatiser. C'est un parcours, pas un catalogue.
        epilog="Premiers pas :\n" + "\n".join(
            f"  {programme} {commande:<20} {intention}"
            for commande, intention in (
                ("login", "se connecter une fois"),
                ("list", "voir ce que contient le module"),
                ("download", "récupérer les clips"),
                ("merge", "assembler les vidéos"),
                ("serve", "ouvrir l'interface"),
                ("autostart on", "surveiller à chaque session"),
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
    parser.add_argument("--hub", help="nom du Sync Module à utiliser")
    parser.add_argument("--camera", help="ne garder que cette caméra")
    parser.add_argument(
        "--since",
        type=runtime.jours_non_negatifs,
        metavar="JOURS",
        help="ne garder que les clips des N derniers jours",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=blink_registre.OUTPUT,
        help=f"dossier de destination (défaut : {blink_registre.OUTPUT})",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="forcer le retéléchargement des clips visibles, même déjà acquis",
    )
    parser.add_argument(
        "--from", dest="source", choices=("usb", "cloud", "all"), default="all",
        help="où chercher les clips : « usb » la clé du module, « cloud » celui "
             "de l'abonnement, « all » les deux (défaut)",
    )
    # Une boucle propre au verbe : le cloud se sonde à la minute sans rien
    # réveiller, là où le manifeste USB mobilise le module et se contente de dix
    # minutes. Deux cadences valent mieux qu'un compromis unique.
    runtime.ajouter_boucle(parser)
    args = parser.parse_args()
    if args.command is None:
        # Sans commande, l'aide plutôt qu'une connexion au compte : une
        # commande tapée sans argument ne doit pas partir sur le réseau.
        parser.print_help()
        raise SystemExit(0)
    return args


async def main(args: argparse.Namespace) -> int:
    # Lazy : seuls login/list/download/start (via ce chemin) paient le coût
    # d'aiohttp et de blinkpy (O-06/8.7/8.8).
    runtime.bootstrap()
    import blink_auth
    import blink_engine
    import blink_models

    async with blink_auth.session_http() as session:
        blink = await blink_auth.connect(session)
        if blink is None:
            print("\nÉchec de la connexion Blink.")
            return 1

        print("\nConnexion Blink réussie.")
        print(f"Session sauvegardée dans : {blink_auth.CONFIG.resolve()}")
        print("\n=== SYNC MODULES ===")
        synchronisations = getattr(blink, "sync", None) or {}
        if not synchronisations:
            print("Aucun Sync Module trouvé sur ce compte.")
        for name, sync in synchronisations.items():
            print(f"- {name} (ID {sync.sync_id}, réseau {sync.network_id})")

        if args.command == "login":
            return 0

        if args.source == "usb" and not synchronisations:
            print("\nLa source USB exige un Sync Module ; utilisez --from cloud.")
            return 1

        try:
            modules = (
                [] if args.source == "cloud"
                else blink_models.select_sync_modules(blink, args.hub)
            )
        except ValueError as error:
            print(f"\nErreur : {error}")
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
        description=ouvrir.__doc__.splitlines()[0],
    )
    parseur.add_argument("--port", type=runtime.port_valide, default=8765,
                         help="port de l'interface (défaut 8765)")
    options = parseur.parse_args(list(arguments))
    adresse = f"http://127.0.0.1:{options.port}/"

    with socket.socket() as prise:
        prise.settimeout(2)
        if prise.connect_ex(("127.0.0.1", options.port)) != 0:
            print(f"Personne n'écoute sur {adresse}.")
            print("Lancez « blink2video serve », ou « blink2video autostart on » "
                  "pour que l'interface démarre avec la session.")
            return 1

    print(f"Ouverture de {adresse}")
    webbrowser.open(adresse)
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
        description=arreter.__doc__.splitlines()[0],
    ).parse_args(list(arguments))

    instances = runtime.lire_instances()
    if not instances:
        print("Rien ne tourne.")
        return 0

    restants = []
    for fiche in instances:
        commande = " ".join(" ".join(groupe) for groupe in fiche.get("verbes") or [])
        print(f"Arrêt de « {commande or 'blink2video'} » "
              f"(PID {fiche['pid']}, depuis {fiche.get('depuis', '?')})")
        # Un numéro de processus fini par être réattribué à un logiciel sans
        # aucun rapport ; le confondre avec l'instance qu'on croit suivre a
        # déjà fait « arrêter » un service tiers et une messagerie sur la
        # machine d'un utilisateur. On vérifie l'identité avant de tuer quoi
        # que ce soit, jamais seulement l'existence du PID.
        membres = [fiche["pid"], *(fiche.get("enfants") or [])]
        for membre in membres:
            if not runtime.processus_vivant(int(membre)):
                continue
            if not runtime.processus_correspond(int(membre)):
                print(f"  PID {membre} ne correspond plus à cette instance "
                      f"(numéro réattribué à un autre logiciel) : ignoré.")
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
        survivants = [str(m) for m in membres if runtime.processus_correspond(int(m))]
        if survivants:
            restants.extend(survivants)
            continue
        Path(fiche["fiche"]).unlink(missing_ok=True)
    if restants:
        print("Toujours en vie : " + ", ".join(restants))
        return 1
    print("Arrêté.")
    return 0


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
        description=redemarrer.__doc__.splitlines()[0],
    )
    parser.add_argument("--finaliser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--sans-relance", action="store_true",
                        help="s'arrêter sans relancer ensuite")
    args = parser.parse_args(list(arguments))
    installe = runtime.app_dir()

    if not args.finaliser:
        suite = ["--finaliser"] + (["--sans-relance"] if args.sans_relance else [])
        runtime.demarrer(runtime.self_command("restart", *suite),
                         cwd=str(installe), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=(sys.platform != "win32"))
        return 0

    runtime.lancer(runtime.self_command("stop"), cwd=str(installe),
                   stdin=subprocess.DEVNULL, check=False)
    # Les fichiers restent tenus quelques instants après la mort du processus,
    # le temps que le système referme ses poignées (même attente que
    # maj.finaliser).
    for _ in range(20):
        if not runtime.lire_instances():
            break
        time.sleep(1)
    if not args.sans_relance:
        runtime.demarrer(runtime.self_command("start"), cwd=str(installe),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         start_new_session=(sys.platform != "win32"))
    return 0


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


def accueillir(etat: dict, supplement: list, delai: float = 600.0) -> int:
    """Onboarding minimal : ouvre l'interface sur sa page de connexion et
    attend un succès avant de laisser « start » continuer (E-01, version
    resserrée : pas de CSRF ni de rotation de port dédiées, l'interface reste
    de toute façon liée à 127.0.0.1 seule).

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
    doit jamais déclencher (section 12.5)."""
    import os
    import webbrowser

    delai = float(os.environ.get("BLINK_ONBOARDING_TIMEOUT", delai))
    port = 8765
    if "--port" in supplement:
        try:
            port = int(supplement[supplement.index("--port") + 1])
        except (IndexError, ValueError):
            pass

    if etat.get("error"):
        print(f"\nSession enregistrée invalide ou injoignable : {etat['error']}")
    print("\nAucune session Blink valide. Ouverture de la page de connexion...")

    processus = runtime.demarrer(
        runtime.self_command("serve", *supplement),
        cwd=str(runtime.app_dir()), creationflags=runtime.flags_enfant(),
        start_new_session=(os.name != "nt"),
    )
    adresse = f"http://127.0.0.1:{port}/?login=1"

    def echec_processus() -> int:
        print("L'interface s'est arrêtée avant la connexion "
              f"(code {processus.returncode}). Abandon.")
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
            print(f"L'interface ne répond pas sur {adresse}. Abandon.")
            return 1

        if os.environ.get("BLINK_NO_BROWSER") == "1":
            print(f"Page de connexion prête sur {adresse} (navigateur non ouvert).")
        else:
            print(f"Ouverture de {adresse}")
            if not webbrowser.open(adresse):
                print(f"Navigateur indisponible : ouvrez {adresse} vous-même, "
                      "ou utilisez « blink2video login » dans un terminal.")

        runtime.bootstrap()
        import blink_auth

        while time.monotonic() < limite:
            if processus.poll() is not None:
                return echec_processus()
            resultat = asyncio.run(blink_auth.preflight())
            if resultat["authenticated"]:
                resume = f"compte accessible, {resultat['cameras']} caméra(s)"
                if resultat["cloud_only"]:
                    resume += " (compte sans Sync Module, cloud uniquement)"
                print(f"\nConnexion réussie : {resume}.")
                return 0
            time.sleep(2)

        print("\nDélai de connexion dépassé. Abandon, rien n'est laissé actif.")
        return 1
    finally:
        if processus.poll() is None:
            runtime.arreter_processus(processus.pid, avec_descendance=True)


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
            print("usage : blink2video start [options de serve]")
            print()
            print("Lance la configuration recommandée :")
            print()
            print("  blink2video " + " ".join(runtime.standard()))
            print()
            print("Les options données ici vont à l'interface, --port par exemple.")
            print("« blink2video stop » arrête l'ensemble.")
            return 0
        # « start » n'est pas un travail de plus : c'est le nom de la
        # composition recommandée, options comprises. Les options données après
        # lui s'ajoutent au premier verbe, « serve », d'où le --port qui marche.
        supplement = groupes[0][1:]
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
                port = runtime.lire_reglages()["port"]
                if _port_ouvert(port):
                    return ouvrir(["--port", str(port)])

                runtime.bootstrap()
                import blink_auth
                etat = asyncio.run(blink_auth.preflight())
                if not etat["authenticated"]:
                    code = accueillir(etat, supplement)
                    if code != 0:
                        return code
                composition = runtime.standard()
                # Le bloc fixe (serve, --port, valeur, --timezone, valeur)
                # précède toujours le supplément : un « --port »/« --timezone »
                # tapé à la main arrive donc après celui, déjà présent, de la
                # configuration enregistrée, et l'emporte (argparse retient la
                # dernière occurrence d'une option).
                n = runtime.LONGUEUR_BLOC_SERVE
                code = executer(runtime.decouper_verbes(
                    [*composition[:n], *supplement, *composition[n:]]))
                if code == 0:
                    _proposer_raccourci_bureau()
                return code
        except runtime.BusyError:
            print("Démarrage déjà en cours ailleurs, ouverture de l'interface...")
            return ouvrir(())

    if len(groupes) == 1 and groupes[0][0] == "open":
        return ouvrir(groupes[0][1:])

    if len(groupes) == 1 and groupes[0][0] == "restart":
        return redemarrer(groupes[0][1:])

    if any(groupe[0] == "stop" for groupe in groupes):
        if len(groupes) > 1:
            print("« stop » s'emploie seul : il arrête ce qui tourne déjà.")
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
                runtime.inscrire_instance(groupes)
            return deleguer(verbe, arguments)
        sys.argv = ["blink2video", verbe, *arguments]
        return asyncio.run(main(parse_args()))

    # Ce qui se termine s'enchaîne, ce qui ne se termine pas tourne à côté.
    # « serve » porte un --loop implicite : il ne rend jamais la main, comme
    # tout verbe à qui on demande de se répéter. Les autres font un passage et
    # s'arrêtent, donc les faire tourner en même temps n'aurait aucun sens :
    # l'assemblage démarrerait pendant que le téléchargement écrit encore.
    persistant = [g for g in groupes if g[0] == "serve" or "--loop" in g]
    ponctuels = [g for g in groupes if g not in persistant]

    runtime.inscrire_instance(groupes)
    lances = []
    for verbe, *arguments in persistant:
        lances.append((verbe, runtime.demarrer(
            runtime.self_command(verbe, *arguments), cwd=str(runtime.app_dir()),
            creationflags=runtime.flags_enfant(),
            # Sa propre session hors Windows : « stop » peut alors tuer son
            # groupe, ffmpeg compris, sans emporter le terminal qui a lancé
            # l'ensemble.
            start_new_session=(sys.platform != "win32"))))
        print(f"Lancé : {verbe} {' '.join(arguments)}".rstrip())
    runtime.inscrire_instance(groupes, [p.pid for _, p in lances])

    # Les passages uniques, l'un après l'autre, dans l'ordre où ils sont cités.
    pire_ponctuel = 0
    for verbe, *arguments in ponctuels:
        print(f"Étape : {verbe} {' '.join(arguments)}".rstrip())
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

    def surveiller(sur_fin=None) -> None:
        nonlocal pire
        while any(processus.poll() is None for _, processus in lances):
            for rang, (verbe, processus) in enumerate(lances):
                code = processus.poll()
                if code is None or rang in annonces:
                    continue
                annonces.add(rang)
                pire = max(pire, abs(code))
                print(f"Arrêté : {verbe}"
                      + (f" (code {code})" if code else " (fin normale)"))
            time.sleep(1)
        if sur_fin:
            sur_fin()

    port = 8765
    for verbe, *arguments in persistant:
        if verbe == "serve" and "--port" in arguments:
            port = int(arguments[arguments.index("--port") + 1])

    import tray

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
                tray.executer(port, fin)
            except Exception:
                # L'icône a échoué en cours de route (backend Linux qui se
                # dérobe, par exemple) : le thread de surveillance, lui,
                # continue, on se contente de l'attendre.
                pass
            veilleur.join()
        else:
            surveiller()
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        for _, processus in lances:
            if processus.poll() is None:
                processus.terminate()
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
        print(f"« {argv[0]} » précède le premier verbe : les options "
              "suivent le verbe auquel elles s'appliquent.")
        print(f"Verbes : {', '.join(runtime.VERBES)}")
        return 2
    sys.argv = ["blink2video", *argv]
    return asyncio.run(main(parse_args()))
