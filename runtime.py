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

import argparse
import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple


# Version de l'outil, et seule source de l'étiquette de publication : le
# workflow de release refuse une étiquette qui ne lui correspond pas. Un binaire
# doit pouvoir dire ce qu'il est, ne serait-ce que pour qu'un rapport de bogue
# soit exploitable.
VERSION = "0.9.9"


# Source unique des verbes : leur ordre d'apparition dans l'aide, le programme
# qui les traite, et ce qu'ils font. Trois usages en découlent, l'aide affichée,
# la délégation d'un verbe au bon programme, et la relance de l'outil sur un
# autre verbe. Les tenir à trois endroits garantissait qu'ils divergent, ce qui
# s'est produit : « autostart » manquait dans une description, « list » et
# « login » n'apparaissaient nulle part dans la liste des verbes.
#
# Le programme « blink » désigne blink2video.py lui-même, qui traite ces verbes sans
# déléguer : le verbe lui est alors passé en argument.
# Chaque verbe porte ses deux libellés : l'aide en ligne prend le français,
# docs.py prend l'un ou l'autre selon le README qu'il remplit. Le README anglais
# a longtemps affiché la liste des verbes en français, faute de cette colonne.
# Nom du point d'entrée, sans extension. Les verbes qu'il traite lui-même le
# désignent par cette constante : le déduire d'un marqueur écrit à la main a
# survécu au renommage du fichier et cassé toute relance, silencieusement.
ENTREE = "blink2video"


# La configuration recommandée, en un seul endroit : « start » la lance,
# « autostart on » la planifie. Chaque activité a sa cadence, parce qu'elles
# n'ont pas le même coût : l'inventaire cloud est un appel de 0,13 s au compte,
# le manifeste USB réveille le module de synchronisation, l'assemblage ne fait
# rien quand rien n'a changé. Verbeux à lire, jamais à taper.
REGLAGES = "blink_reglages.json"
REGLAGES_DEFAUT = {"usb_minutes": 10, "cloud_minutes": 1, "port": 8765, "timestamp": True,
                   "timezone": "Europe/Paris", "merge_jour": True, "merge_semaine": True,
                   "merge_mois": True, "download_auto": True}
# Nombre d'éléments du bloc fixe en tête de standard() : serve, --port,
# valeur, --timezone, valeur. blink_cli.route() s'appuie sur cette longueur
# pour greffer le supplément de « start » juste après (voir standard()).
LONGUEUR_BLOC_SERVE = 5


def _entier_borne(valeurs: dict, champ: str, defaut: int, minimum: int,
                  maximum: int | None = None) -> int:
    """Lit un entier borné dans le JSON des réglages, replie sur `defaut`
    dès que la valeur est absente, du mauvais type, ou hors plage.

    Mêmes bornes que la validation déjà faite côté écriture (`/api/reglages`
    dans serve.py) : ce plancher/plafond n'est pas nouveau, seule sa lecture
    l'est - un fichier modifié à la main ou corrompu (revue de code du
    0eab463, bug #10) ne pouvait jusqu'ici passer par aucun de ces deux
    contrôles, contrairement au formulaire web."""
    try:
        nombre = int(valeurs.get(champ, defaut))
    except (TypeError, ValueError):
        return defaut
    if nombre < minimum or (maximum is not None and nombre > maximum):
        return defaut
    return nombre


def _booleen(valeurs: dict, champ: str, defaut: bool) -> bool:
    """Lit un booléen, replie sur `defaut` si ce n'en est pas un.

    `bool(valeur)` seul rend presque tout vrai (seuls 0, "", None, [], {}
    sont faux en Python) : une chaîne "false" - une erreur plausible en
    éditant le JSON à la main, puisque JSON exige `false` sans guillemets -
    y devenait donc `True`, l'inverse de l'intention (revue de code du
    0eab463, bug #10). Seul un vrai booléen JSON (`true`/`false` sans
    guillemets) est désormais accepté ; tout le reste retombe sur le
    défaut plutôt que d'être mal interprété en silence."""
    valeur = valeurs.get(champ, defaut)
    return valeur if isinstance(valeur, bool) else defaut


def lire_reglages() -> dict:
    """Cadences USB/cloud, port, horodatage et fuseau actuels, modifiables
    depuis la page web.

    Fichier absent ou illisible : les valeurs par défaut, identiques à
    celles qui étaient figées en dur ici avant que ce réglage existe."""
    try:
        valeurs = json.loads((app_dir() / REGLAGES).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(REGLAGES_DEFAUT)
    if not isinstance(valeurs, dict):
        # JSON valide (`[]`, `"texte"`, `42`...) mais pas un objet : aussi
        # inexploitable qu'illisible (revue de code du 0eab463, bug #10 -
        # `.get()` sur autre chose qu'un dict levait AttributeError, jamais
        # intercepté).
        return dict(REGLAGES_DEFAUT)
    return {
        "usb_minutes": _entier_borne(valeurs, "usb_minutes",
                                     REGLAGES_DEFAUT["usb_minutes"], 1),
        "cloud_minutes": _entier_borne(valeurs, "cloud_minutes",
                                       REGLAGES_DEFAUT["cloud_minutes"], 1),
        "port": _entier_borne(valeurs, "port", REGLAGES_DEFAUT["port"], 1, 65535),
        "timestamp": _booleen(valeurs, "timestamp", REGLAGES_DEFAUT["timestamp"]),
        "timezone": str(valeurs.get("timezone", REGLAGES_DEFAUT["timezone"])) or
        REGLAGES_DEFAUT["timezone"],
        "merge_jour": _booleen(valeurs, "merge_jour", REGLAGES_DEFAUT["merge_jour"]),
        "merge_semaine": _booleen(valeurs, "merge_semaine", REGLAGES_DEFAUT["merge_semaine"]),
        "merge_mois": _booleen(valeurs, "merge_mois", REGLAGES_DEFAUT["merge_mois"]),
        "download_auto": _booleen(valeurs, "download_auto", REGLAGES_DEFAUT["download_auto"]),
    }


def ecrire_reglages(usb_minutes: int, cloud_minutes: int, port: int, timestamp: bool,
                    timezone: str, merge_jour: bool, merge_semaine: bool,
                    merge_mois: bool, download_auto: bool) -> None:
    # Écriture atomique (temporaire propre à ce processus, puis replace) :
    # même précaution que blink_auth.save_session (I-02) - un plantage en
    # cours d'écriture ne doit jamais laisser un JSON à moitié écrit, que
    # lire_reglages() prendrait pour un fichier corrompu et remplacerait
    # entièrement par les défauts (revue de code du 0eab463, bug #10).
    import uuid

    cible = app_dir() / REGLAGES
    temporaire = cible.with_name(f"{cible.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        temporaire.write_text(
            json.dumps({"usb_minutes": int(usb_minutes), "cloud_minutes": int(cloud_minutes),
                        "port": int(port), "timestamp": bool(timestamp),
                        "timezone": str(timezone), "merge_jour": bool(merge_jour),
                        "merge_semaine": bool(merge_semaine), "merge_mois": bool(merge_mois),
                        "download_auto": bool(download_auto)}),
            encoding="utf-8")
        temporaire.replace(cible)
    finally:
        temporaire.unlink(missing_ok=True)


LANGUE = "blink_langue.txt"


def lire_langue() -> str:
    """Langue de la dernière page web chargée, « fr » ou « en ».

    Fichier absent (rien encore chargé) ou illisible : « fr », le même
    défaut que detectLang() côté navigateur quand la langue système n'est
    pas reconnue. Sert au menu du systray (tray.py) pour hériter de la
    langue de la page plutôt que de rester figé en français."""
    try:
        contenu = (app_dir() / LANGUE).read_text(encoding="utf-8").strip()
    except OSError:
        return "fr"
    return "en" if contenu == "en" else "fr"


def ecrire_langue(code: str) -> None:
    """Mémorise la langue affichée par la page web (POST /api/lang), à
    chaque chargement ou changement explicite - pas seulement le choix
    manuel : sans ça, un premier lancement jamais retouché aux boutons
    FR/EN laisserait le menu du systray dans le défaut, même si la page
    s'affichait déjà en anglais (langue détectée du navigateur)."""
    import uuid

    cible = app_dir() / LANGUE
    temporaire = cible.with_name(f"{cible.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        temporaire.write_text("en" if code == "en" else "fr", encoding="utf-8")
        temporaire.replace(cible)
    finally:
        temporaire.unlink(missing_ok=True)


SUPPRESSION_AUTO = "blink_suppression_auto.json"


def lire_suppression_auto() -> set:
    """Caméras dont les clips USB sont supprimés du Sync Module une fois
    téléchargés avec succès (issue GitHub #1 : libérer la mémoire tampon).

    Par caméra, pas globalement : une caméra encore incertaine (peu de recul
    sur la fiabilité du téléchargement) doit pouvoir rester en conservation
    pendant qu'une autre, déjà éprouvée, libère sa mémoire. Fichier absent ou
    illisible : ensemble vide, personne n'est supprimé par défaut."""
    import json

    try:
        contenu = (app_dir() / SUPPRESSION_AUTO).read_text(encoding="utf-8")
    except OSError:
        return set()
    try:
        cameras = json.loads(contenu)
    except json.JSONDecodeError:
        return set()
    if not isinstance(cameras, list):
        return set()
    return {str(c) for c in cameras if isinstance(c, str)}


def ecrire_suppression_auto(cameras: set) -> None:
    """Enregistre l'ensemble des caméras en suppression automatique (USB)."""
    import json
    import uuid

    cible = app_dir() / SUPPRESSION_AUTO
    temporaire = cible.with_name(f"{cible.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        temporaire.write_text(json.dumps(sorted(cameras), ensure_ascii=False), encoding="utf-8")
        temporaire.replace(cible)
    finally:
        temporaire.unlink(missing_ok=True)


def standard() -> tuple:
    """Composition recommandée : mêmes verbes que l'ancienne constante
    STANDARD, mais les cadences USB/cloud, le port, l'horodatage et le
    fuseau viennent de `lire_reglages()` plutôt que d'être figés ici, pour
    que le réglage depuis la page web prenne effet au prochain démarrage.

    Les LONGUEUR_BLOC_SERVE premiers éléments (serve, --port, valeur,
    --timezone, valeur) sont un bloc fixe : blink_cli.route() les traite
    comme la partie « verbe » sur laquelle un supplément tapé à la main
    (« start --port 8899 ») se greffe, afin qu'un --port ou --timezone
    explicite l'emporte toujours sur la valeur enregistrée (argparse
    retient la dernière occurrence d'une option)."""
    c = lire_reglages()
    merge = []
    if c["merge_jour"]:
        merge = ["merge", "--loop", "5", "--timezone", c["timezone"]]
        if not c["timestamp"]:
            merge.append("--no-timestamp")
        if not c["merge_semaine"]:
            merge.append("--no-weekly")
        if not c["merge_mois"]:
            merge.append("--no-monthly")
    download = []
    if c["download_auto"]:
        download = ["download", "--from", "usb", "--loop", str(c["usb_minutes"]),
                    "download", "--from", "cloud", "--loop", str(c["cloud_minutes"])]
    return ("serve", "--port", str(c["port"]), "--timezone", c["timezone"],
            "watch", "--loop", "10",
            *download, *merge)


class Verbe(NamedTuple):
    """Un verbe : le programme qui l'exécute, et son libellé dans chaque langue.

    Un tuple nommé plutôt qu'un tuple simple parce que la table est lue depuis
    cinq endroits, dont un fichier .spec : le jour où une troisième colonne est
    apparue, tous ceux qui déballaient « module, texte » ont cassé d'un coup.
    Nommer les champs supprime la classe entière de ces pannes."""

    module: str
    fr: str
    en: str


VERBES = {
    "login": Verbe(ENTREE,
              "se connecter au compte Blink, vérification en deux étapes gérée",
              "sign in to the Blink account, two-step verification handled"),
    "list": Verbe(ENTREE,
             "ce que contient le module de synchronisation en ce moment",
             "what the Sync Module currently holds"),
    "download": Verbe(ENTREE,
                 "récupérer les nouveaux clips avant que la rotation ne les efface",
                 "fetch new clips before rotation erases them"),
    "merge": Verbe("merge_daily",
              "normaliser, horodater et assembler jour, semaine et mois",
              "normalize, stamp and assemble day, week and month"),
    "watch": Verbe("watch",
              "contrôler l'état de l'installation et alerter s'il se dégrade",
              "check the installation and alert when it degrades"),
    "serve": Verbe("serve",
              "servir l'interface web, pour regarder, écarter, voir en direct",
              "serve the web interface, to watch, discard, see live"),
    "start": Verbe(ENTREE,
             "tout lancer avec les réglages recommandés",
             "start everything with the recommended settings"),
    "open": Verbe(ENTREE,
            "ouvrir l'interface web dans le navigateur",
            "open the web interface in the browser"),
    "stop": Verbe(ENTREE,
            "arrêter l'instance qui tourne en fond",
            "stop the instance running in the background"),
    "restart": Verbe(ENTREE,
               "arrêter puis relancer avec les réglages actuels",
               "stop then relaunch with the current settings"),
    "update": Verbe("maj",
              "installer la dernière version publiée",
              "install the latest published release"),
    "autostart": Verbe("autostart",
                  "inscrire à l'ouverture de session la commande qui suit",
                  "register the command that follows with your session"),
    "smoketest": Verbe("smoketest",
                  "vérifier que l'installation fonctionne sur cette machine",
                  "check that the installation works on this machine"),
}

# Verbes confiés à un autre programme, déduits de la table ci-dessus.
DELEGUES = {nom: verbe.module for nom, verbe in VERBES.items()
            if verbe.module != ENTREE}


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
      auto (défaut) : venv dans ~/.blink2video/venv, créé au besoin, puis relance
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

    venv_dir = Path.home() / ".blink2video" / "venv"
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


POINTEUR_STOCKAGE = "blink_home.txt"


def _dossier_ancre() -> Path:
    """Emplacement par défaut, celui d'avant tout réglage : à côté de
    l'exécutable (figé) ou des sources. Fixe, jamais lui-même redirigé -
    c'est justement ce qui permet d'y chercher `POINTEUR_STOCKAGE` sans
    dépendre de la valeur qu'il contient (sinon, pour savoir où lire le
    réglage, il faudrait déjà connaître ce que le réglage doit dire)."""
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def app_dir() -> Path:
    """Dossier des données : clips, vidéos, registres, journaux.

    Trois façons de le déplacer, dans cet ordre : la variable d'environnement
    BLINK_HOME (utile pour une installation en lecture seule), le fichier
    `blink_home.txt` à côté de l'exécutable (réglable depuis la page web,
    voir `lire_dossier_stockage`/`ecrire_dossier_stockage`), et à défaut
    l'emplacement historique, celui de l'exécutable lui-même."""
    forced = os.environ.get("BLINK_HOME")
    if forced:
        return Path(forced).expanduser().resolve()
    pointeur = _dossier_ancre() / POINTEUR_STOCKAGE
    try:
        cible = pointeur.read_text(encoding="utf-8").strip()
    except OSError:
        cible = ""
    if cible:
        return Path(cible).expanduser().resolve()
    return _dossier_ancre()


def lire_dossier_stockage() -> str:
    """Dossier de données effectif, tel qu'affiché dans le panneau de
    réglages : la valeur réelle (app_dir()), pas le contenu brut du
    pointeur, absent tant que personne n'a rien changé."""
    return str(app_dir())


def ecrire_dossier_stockage(chemin: str) -> None:
    """Enregistre le nouveau dossier, ou efface le réglage si `chemin` est
    vide (retour à l'emplacement par défaut).

    Ne déplace rien : les fichiers déjà présents à l'ancien emplacement y
    restent, à charge de qui change ce réglage de les reprendre lui-même -
    choix délibéré, un déplacement automatique engageant bien plus (espace
    disque, fichiers ouverts, échec à mi-chemin) pour un réglage qui change
    rarement."""
    pointeur = _dossier_ancre() / POINTEUR_STOCKAGE
    chemin = chemin.strip()
    if not chemin:
        pointeur.unlink(missing_ok=True)
        return
    pointeur.write_text(chemin, encoding="utf-8")


def resource_dir() -> Path:
    """Dossier des ressources embarquées dans le bundle."""
    if frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    return Path(__file__).resolve().parent


# Une fiche par instance en cours, nommée d'après le processus qui la tient.
# Un fichier unique aurait suffi tant qu'une seule instance tourne, mais rien ne
# l'interdit : celle du démarrage automatique et une autre lancée à la main
# coexistent très bien, et « stop » doit pouvoir les arrêter toutes.
INSTANCES = Path(".blink_run")


def processus_vivant(pid: int) -> bool:
    """Vrai si ce numéro désigne un processus existant.

    Un numéro est réattribué après un certain temps : la fiche porte donc aussi
    l'heure de départ, et une fiche dont le processus a disparu est effacée à la
    première lecture."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        # Un processus tué reste « zombie » jusqu'à ce que son parent le
        # récupère, et le signal zéro lui parvient encore : sans ce second
        # examen, « stop » annonce que sa cible a survécu alors qu'elle est
        # morte, ce qu'un runner a montré aussitôt.
        etat = lancer(["ps", "-o", "state=", "-p", str(pid)],
                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                      stderr=subprocess.DEVNULL, text=True, errors="replace",
                      check=False)
        return not (etat.stdout or "").strip().startswith("Z")
    resultat = lancer(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                      stderr=subprocess.DEVNULL, text=True, errors="replace",
                      check=False)
    return str(pid) in (resultat.stdout or "")


def ligne_de_commande(pid: int) -> str:
    """Ligne de commande du processus, ou chaîne vide si illisible.

    Un nom d'image seul ne suffit pas à confirmer une identité : deux
    python.exe peuvent coexister sans aucun rapport entre eux. La ligne de
    commande, elle, porte le chemin du script lancé."""
    if pid <= 0:
        return ""
    if os.name != "nt":
        resultat = lancer(["ps", "-o", "args=", "-p", str(pid)],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, errors="replace",
                          check=False)
        return (resultat.stdout or "").strip()
    # « wmic », déprécié, disparaît des images Windows récentes ; Get-CimInstance
    # est son remplaçant courant pour interroger un processus par PID.
    resultat = lancer(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' "
         f"-ErrorAction SilentlyContinue).CommandLine"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, errors="replace", check=False,
    )
    return (resultat.stdout or "").strip()


def _empreintes_attendues() -> list:
    """Ce qu'on doit retrouver dans la ligne de commande d'un vrai processus
    de ce projet — le point d'entrée lui-même, ou l'un de ses sous-verbes
    (serve.py, watch.py, merge_daily.py...).

    Deux marqueurs, pas un seul : le point d'entrée porte le nom du projet
    dans sa ligne de commande quelle que soit la façon dont l'utilisateur l'a
    tapée (chemin relatif, absolu, bundle), mais les sous-verbes lancés
    depuis les sources sont des scripts différents (voir self_command) qui ne
    portent pas ce nom ; seul leur dossier commun l'est, de façon fiable,
    puisqu'ils se relancent eux-mêmes avec un chemin absolu."""
    if frozen():
        return ["blink2video"]
    return ["blink2video", str(Path(__file__).resolve().parent)]


def processus_correspond(pid: int, marqueurs: list | None = None) -> bool:
    """Vrai si ce PID désigne bien un processus de ce projet.

    Un numéro de processus fini par être réattribué à un logiciel sans
    aucun rapport ; le confondre avec l'instance qu'on croit suivre a déjà
    fait « arrêter » un service HP, un terminal et une messagerie sur la
    machine d'un utilisateur, simplement parce qu'un numéro coïncidait.
    Vérifier l'existence du PID ne suffit pas : il faut vérifier son
    identité avant d'y toucher, *a fortiori* avant de le tuer."""
    if not processus_vivant(pid):
        return False
    ligne = ligne_de_commande(pid)
    return any(m in ligne for m in (marqueurs or _empreintes_attendues()))


def inscrire_instance(entrees: list, enfants=None) -> Path:
    """Dépose la fiche de l'instance courante, et la retire à sa mort.

    Sans elle, arrêter une instance lancée sans console reviendrait à chercher
    des numéros de processus à la main, puis à tuer un arbre en espérant
    n'oublier personne."""
    import atexit
    import datetime as dt

    dossier = app_dir() / INSTANCES
    dossier.mkdir(parents=True, exist_ok=True)
    fiche = dossier / f"{os.getpid()}.json"
    fiche.write_text(json.dumps({
        "pid": os.getpid(),
        "depuis": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "verbes": entrees,
        # Les enfants sont notés pour pouvoir vérifier, après l'arrêt, qu'aucun
        # n'a survécu : c'est précisément ce qui se produisait avant.
        "enfants": list(enfants or []),
    }, ensure_ascii=False), encoding="utf-8")
    atexit.register(lambda: fiche.unlink(missing_ok=True))
    return fiche


def lire_instances() -> list:
    """Fiches des instances réellement en cours, les périmées étant effacées.

    I-13 : un parent mort ne suffit pas à dire l'instance terminée. Un enfant
    persistant peut lui survivre (le parent supervise, mais ne fait pas
    partie du même groupe de processus que ses enfants sous POSIX), et
    « stop » a besoin de la fiche pour retrouver ce survivant. On n'efface
    donc que les fiches dont ni le parent, ni aucun enfant, ne vivent plus."""
    dossier = app_dir() / INSTANCES
    vivantes = []
    for fiche in sorted(dossier.glob("*.json")) if dossier.is_dir() else []:
        try:
            donnees = json.loads(fiche.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            fiche.unlink(missing_ok=True)
            continue
        membres = [donnees.get("pid"), *(donnees.get("enfants") or [])]
        if any(processus_correspond(int(membre or 0)) for membre in membres):
            donnees["fiche"] = fiche
            vivantes.append(donnees)
        else:
            fiche.unlink(missing_ok=True)
    return vivantes


def arreter_processus(pid: int, avec_descendance: bool = False) -> None:
    """Arrête un processus, et sa descendance si on le demande.

    La distinction est vitale hors Windows : un verbe lancé côte à côte reçoit
    sa propre session (voir flags_enfant), on peut donc tuer son groupe entier,
    ffmpeg compris. Le parent, lui, partage le groupe du terminal qui l'a lancé
    : lui appliquer le même traitement tuerait ce terminal. C'est arrivé, et la
    victime a été la suite de tests elle-même, sur les runners.

    Sous Windows la distinction compte tout autant : « taskkill /T » descend
    l'arbre du PID donné tel que Windows le voit, ce qui inclut n'importe quel
    processus qui s'est retrouvé enfant du nôtre sans être un de nos workers,
    par exemple le navigateur ouvert par webbrowser.open() quand aucune
    fenêtre n'existait encore. Un « stop »/« restart » sur le processus
    principal (avec_descendance=False) a fermé Chrome en entier de cette
    façon : /T ne doit s'appliquer qu'à un enfant qu'on sait être un des
    nôtres (avec_descendance=True)."""
    if os.name == "nt":
        commande = ["taskkill", "/F", "/PID", str(pid)]
        if avec_descendance:
            commande.insert(2, "/T")
        lancer(commande, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
               stderr=subprocess.DEVNULL, check=False)
        return
    import signal

    def envoyer(signal_) -> bool:
        try:
            if avec_descendance:
                os.killpg(os.getpgid(pid), signal_)
            else:
                os.kill(pid, signal_)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True

    if not envoyer(signal.SIGTERM):
        return
    for _ in range(20):
        if not processus_vivant(pid):
            return
        time.sleep(0.25)
    envoyer(signal.SIGKILL)


# Sous Windows, tout programme lancé depuis une application sans console en
# ouvre une le temps de son exécution : une fenêtre noire qui apparaît et
# disparaît à chaque vignette, à chaque analyse de vidéo, à chaque notification.
# Ce drapeau l'en empêche. Ailleurs il n'existe pas et vaut zéro.
SANS_FENETRE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def console_disponible() -> bool:
    """Vrai si ce processus a une console où écrire.

    Faux sous pythonw.exe, c'est-à-dire quand blink est lancé par l'entrée de
    démarrage automatique."""
    if os.name != "nt":
        return True
    import ctypes

    return bool(ctypes.windll.kernel32.GetConsoleWindow())


def flags_enfant() -> int:
    """Drapeaux pour un verbe lancé côte à côte avec d'autres.

    Il doit parler dans la console de son parent quand il y en a une, sans quoi
    ses erreurs se perdent : « serve » refusant un port déjà pris ne disait
    rien, et l'échec restait incompréhensible. Quand il n'y en a pas, on garde
    CREATE_NO_WINDOW, faute de quoi Windows en ouvre une par verbe, ces
    fenêtres noires qui clignotent."""
    return 0 if console_disponible() else SANS_FENETRE


def lancer(commande, **options):
    """subprocess.run, sans jamais faire clignoter de fenêtre sous Windows."""
    options.setdefault("creationflags", SANS_FENETRE)
    return subprocess.run(commande, **options)


def demarrer(commande, **options):
    """subprocess.Popen, même précaution."""
    options.setdefault("creationflags", SANS_FENETRE)
    return subprocess.Popen(commande, **options)


def _applescript(texte: str) -> str:
    """Encode une chaîne pour AppleScript, dont l'échappement n'est pas celui
    d'un shell : seules les guillemets et la barre oblique inverse comptent."""
    return '"' + texte.replace("\\", "\\\\").replace('"', '\\"') + '"'



def _declarer_identite() -> None:
    """Déclare l'identité applicative auprès de Windows, une fois pour toutes.

    Sans elle, la notification est acceptée par l'API et jetée par le
    système, sans erreur ni trace. La clé vit dans la ruche de
    l'utilisateur, ne concerne que les notifications, et se retire en la
    supprimant :

        HKCU\\SOFTWARE\\Classes\\AppUserModelId\\blink2video
    """
    if sys.platform != "win32":
        return
    try:
        import winreg
    except ImportError:
        return
    chemin = 'SOFTWARE\\Classes\\AppUserModelId' + '\\' + APP_ID
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, chemin) as cle:
            if winreg.QueryValueEx(cle, "DisplayName")[0] == APP_ID:
                return
    except OSError:
        pass
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, chemin) as cle:
            winreg.SetValueEx(cle, "DisplayName", 0, winreg.REG_SZ, APP_ID)
    except OSError:
        # Registre en lecture seule : la notification échouera peut-être,
        # ce n'est pas une raison d'interrompre le travail en cours.
        pass


def toast(titre: str, corps: str, url: str = "") -> None:
    """Notification Windows non bloquante, sans rien installer.

    Distincte du popup à dessein : une coupure est rare et doit être vue, donc
    elle bloque jusqu'à acquittement ; l'arrivée d'un clip est fréquente et
    banale, une fenêtre modale à chaque fois serait insupportable.

    Passe par PowerShell, qui expose l'API de notification de Windows 10. Ça
    évite d'ajouter une dépendance pour une dizaine de lignes, et de réécrire
    à la main un icône de zone de notification en Win32."""
    if sys.platform == "darwin":
        lancer(
            ["osascript", "-e",
             f"display notification {_applescript(corps)} with title {_applescript(titre)}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=20,
        )
        return
    if sys.platform.startswith("linux"):
        if shutil.which("notify-send"):
            lancer(["notify-send", titre, corps],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False, timeout=20)
            return
    if sys.platform != "win32":
        print(f"{titre} : {corps}")
        return
    _declarer_identite()

    def echappe(valeur: str) -> str:
        return (valeur.replace("&", "&amp;").replace("<", "&lt;")
                      .replace(">", "&gt;").replace("'", "&apos;"))

    lancement = (f' activationType="protocol" launch="{echappe(url)}"') if url else ""
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
        " ContentType = WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom,"
        " ContentType = WindowsRuntime] | Out-Null;"
        "$x = [Windows.Data.Xml.Dom.XmlDocument]::new();"
        # activationType=protocol : Windows ouvre l'URL au clic, sans qu'on ait
        # à enregistrer un gestionnaire d'activation. C'est ce qui permet de
        # passer directement du « nouveau clip » à la page qui l'affiche.
        f"$x.LoadXml('<toast{lancement}><visual><binding template=\"ToastGeneric\">"
        f"<text>{echappe(titre)}</text><text>{echappe(corps)}</text>"
        "</binding></visual></toast>');"
        "$n = [Windows.UI.Notifications.ToastNotification]::new($x);"
        # Une notification doit être émise au nom d'une application déclarée
        # auprès de Windows. Plutôt que d'en enregistrer une, on emprunte
        # l'identité de PowerShell, déjà déclarée sur toute installation.
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        + repr(APP_ID) +
        ").Show($n)"
    )
    lancer(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False, timeout=20,
    )



class BusyError(RuntimeError):
    """La ressource est déjà réservée par une autre opération."""


def _lire_verrou(fichier: Path):
    """Contenu du fichier de verrou, ou None s'il est absent/illisible.

    Illisible couvre aussi bien la corruption que la fenêtre, très brève, où un
    autre processus vient de créer le fichier sans avoir fini d'y écrire : dans
    les deux cas on ne peut rien affirmer sur son propriétaire, donc on
    retente plutôt que de trancher à tort."""
    try:
        return json.loads(fichier.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


@contextlib.contextmanager
def verrou(nom: str, owner: str, stale_after: int = 600, attente: int = 0):
    """Réserve une ressource entre processus, le temps d'une opération.

    Deux ressources ne se partagent pas : le Sync Module, qui ne traite qu'une
    commande à la fois et répond « System is busy » à la seconde, et
    l'assemblage, qui écrirait deux fois les mêmes fichiers. Les verbes tournant
    dans des processus séparés, un verrou mémoire ne suffit pas : il faut une
    marque sur disque.

    L'acquisition passe par une création exclusive du fichier
    (``O_CREAT | O_EXCL``) : deux processus qui la tentent en même temps ne
    peuvent pas réussir tous les deux, l'OS le garantit. L'ancien protocole
    lisait le fichier, concluait qu'il était libre, puis l'écrivait : rien
    n'empêchait deux processus de franchir les deux premières étapes ensemble
    (B-05). Un jeton d'acquisition propre à cet appel accompagne owner/pid : il
    sert à ne jamais libérer, à la sortie, un verrou qu'un autre processus
    aurait entre-temps re-acquis sous le même PID recyclé.

    Un seul garde-fou reste appliqué contre une marque oubliée après un
    plantage : on ignore celle dont le processus n'existe plus. `stale_after`
    ne vole en revanche jamais le verrou d'un processus vivant sur le seul
    critère de son âge (B-05) : un passage de téléchargement peut légitimement
    dépasser dix minutes sur un gros clip, et un âge à lui seul ne prouve rien.
    Reste ouvert le cas d'un PID recyclé par un processus non lié pendant que
    le vrai propriétaire est mort : sans l'heure de création du processus,
    non disponible ici sans dépendance supplémentaire, ce cas ne peut être
    tranché automatiquement et n'est donc pas couvert (limite documentée,
    voir AUDIT-2026-08-13.md).

    `attente` donne le temps pendant lequel on réessaie avant de renoncer. Un
    direct dure quelques minutes ; sans attente, une boucle qui tombe dessus
    perdrait son tour entier, alors que la ressource se libère souvent en
    quelques secondes."""
    import uuid

    fichier = app_dir() / f".blink_{nom}.lock"
    jeton = uuid.uuid4().hex
    limite = time.time() + max(attente, 0)
    contenu = json.dumps(
        {"owner": owner, "pid": os.getpid(), "jeton": jeton, "at": time.time()}
    ).encode("utf-8")

    while True:
        try:
            descripteur = os.open(fichier, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pass
        else:
            # La création a réussi : le verrou nous appartient, personne
            # d'autre n'a pu obtenir le même fichier au même instant.
            with os.fdopen(descripteur, "wb") as flux:
                flux.write(contenu)
            break

        presente = _lire_verrou(fichier)
        if presente is None:
            if fichier.exists():
                # Présent mais illisible : corrompu, ou fenêtre d'écriture
                # d'un autre processus pas encore terminée. Les deux se
                # confondent ici (même limite documentée que pour B-05), mais
                # doivent quand même respecter `attente` - une marque restée
                # corrompue (écriture interrompue, disque en cause) tournait
                # sinon en boucle active, sans jamais lever BusyError ni
                # rendre la main (revue de code du 0eab463, bug #3).
                if time.time() >= limite:
                    raise BusyError(f"verrou illisible ou corrompu : {fichier}")
                time.sleep(0.05)
                continue
            # Fichier réellement absent entre l'échec de la création et la
            # lecture (son propriétaire vient de le libérer) : retenter tout
            # de suite, rien à attendre.
            continue
        if not processus_vivant(int(presente.get("pid") or 0)):
            # Propriétaire mort : purge sous mutex, pas par un unlink direct.
            # L'ancien protocole (lire, conclure « mort », supprimer) laissait
            # une fenêtre entre la lecture et la suppression : un second
            # processus arrivé au même verdict pouvait y supprimer, à la
            # place de la marque périmée qu'il croyait viser, celle qu'un
            # premier processus venait de recréer entre-temps - les deux se
            # croyaient alors propriétaires (revue de code du 0eab463, bug
            # #3). Le fichier `.purge` sert exactement le même rôle que
            # O_CREAT|O_EXCL sur le verrou lui-même : un seul processus à la
            # fois entre dans la section suivante, et celui qui y entre
            # revérifie que le jeton lu n'a pas changé avant de supprimer.
            purge = fichier.with_name(fichier.name + ".purge")
            try:
                os.close(os.open(purge, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            except FileExistsError:
                # Un autre processus est dans cette même section (ou, très
                # rarement, y est mort avant son finally) : `limite` reste le
                # seul garde-fou contre une attente indéfinie, même ici.
                if time.time() >= limite:
                    raise BusyError(f"purge du verrou déjà en cours pour "
                                    f"« {presente.get('owner')} »")
                time.sleep(0.05)
                continue
            try:
                encore = _lire_verrou(fichier)
                if encore is not None and encore.get("jeton") == presente.get("jeton"):
                    fichier.unlink(missing_ok=True)
            finally:
                purge.unlink(missing_ok=True)
            continue

        if time.time() >= limite:
            age = time.time() - float(presente.get("at") or 0)
            raise BusyError(f"déjà réservé par « {presente.get('owner')} » "
                            f"depuis {int(age)} s")
        time.sleep(1)

    try:
        yield
    finally:
        courante = _lire_verrou(fichier)
        if courante is not None and courante.get("jeton") == jeton:
            fichier.unlink(missing_ok=True)


# Identité applicative sous laquelle les notifications Windows sont émises.
# Windows jette en silence, en rendant zéro, toute notification dont
# l'identité n'est pas déclarée : emprunter celle de PowerShell marchait sur
# certaines installations et pas sur d'autres, sans jamais dire pourquoi. On
# déclare donc la nôtre, comme le font Firefox ou Acrobat.
APP_ID = "blink2video"


PASSAGES = Path(".blink_passages.json")


def marquer(verbe: str) -> None:
    """Note l'heure à laquelle un verbe vient de finir son travail.

    Deux usages : l'interface affiche ces heures, ce qui rend visible d'un coup
    d'œil une boucle qui ne tourne plus, et un verbe évite de refaire ce qu'un
    autre vient de faire."""
    import datetime as dt

    fichier = app_dir() / PASSAGES
    passages = {}
    try:
        passages = json.loads(fichier.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        passages = {}
    passages[verbe] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        fichier.write_text(json.dumps(passages, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def passages() -> dict:
    """Heure du dernier passage de chaque verbe."""
    try:
        return json.loads((app_dir() / PASSAGES).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def passage_recent(verbe: str, minutes: float) -> bool:
    """Vrai si ce verbe est passé il y a moins de `minutes`."""
    import datetime as dt

    quand = passages().get(verbe)
    if not quand:
        return False
    try:
        moment = dt.datetime.fromisoformat(quand)
    except ValueError:
        return False
    return (dt.datetime.now().astimezone() - moment).total_seconds() < minutes * 60


TRAVAIL = Path(".blink_travail.json")


def travail(quoi: str, fait: float = 0, total: int = 0, cle: str | None = None) -> None:
    """Publie l'avancement du travail en cours, pour qui voudrait le montrer.

    Les verbes tournent dans leurs propres processus : quand l'assemblage part
    d'une boucle de fond, l'interface ne voit rien de sa sortie. Ce fichier est
    le seul point où elle peut apprendre qu'un calcul occupe la machine, et où
    il en est. Écrit à chaque clip, effacé à la fin.

    `quoi` reste le texte français en dur, pour les appelants qui n'affichent
    rien (journaux, terminal) : ne pas leur imposer de traduction. `cle` est
    une étiquette stable, optionnelle, que la page web utilise pour retrouver
    l'équivalent anglais dans son propre dictionnaire (I18N) sans que ce
    fichier n'ait besoin d'en connaître la traduction lui-même - un texte
    inconnu (clé absente, ou clé que la page ne reconnaît pas) reste affiché
    tel quel, jamais une chaîne vide."""
    import datetime as dt

    etat = {"quoi": quoi, "cle": cle, "fait": round(fait, 3), "total": total,
            "pid": os.getpid(),
            "depuis": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    try:
        (app_dir() / TRAVAIL).write_text(json.dumps(etat, ensure_ascii=False),
                                         encoding="utf-8")
    except OSError:
        pass


def fin_travail() -> None:
    """Retire notre marque d'avancement, si c'est bien la nôtre."""
    en_cours = travail_en_cours()
    if not en_cours or en_cours.get("pid") == os.getpid():
        try:
            (app_dir() / TRAVAIL).unlink(missing_ok=True)
        except OSError:
            pass


def travail_en_cours() -> dict:
    """Le travail en cours, ou un dictionnaire vide.

    Mêmes garde-fous que les verrous : une marque laissée par un processus mort
    ou trop vieille ne doit pas faire croire à un calcul éternel."""
    import datetime as dt

    try:
        etat = json.loads((app_dir() / TRAVAIL).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(etat, dict) or not processus_vivant(int(etat.get("pid") or 0)):
        return {}
    try:
        depuis = dt.datetime.fromisoformat(str(etat.get("depuis")))
    except ValueError:
        return {}
    if (dt.datetime.now().astimezone() - depuis).total_seconds() > 900:
        return {}
    return etat


# I-11 : liste des options qui attendent une valeur, pour ne jamais confondre
# cette valeur avec le début d'un nouveau groupe même si elle porte le même
# texte qu'un verbe (« download --camera watch », « download --camera
# update »...). Tenue ici plutôt que déduite des analyseurs de chaque
# programme : ceux-ci vivent dans des fichiers séparés (blink2video.py,
# merge_daily.py, serve.py, watch.py), que ce découpage précède et ne peut
# pas encore interroger.
_OPTIONS_VALEUR_UNIQUE = frozenset((
    "--hub", "--camera", "--since", "--output", "--from", "--port",
    "--input", "--weekly-output", "--monthly-output", "--normalized-output",
    "--excluded-output", "--timezone", "--date", "--font", "--preset",
    "--crf", "--thumbs",
))
# Options « nargs=+ » : consomment tous les mots qui suivent tant qu'aucun
# ne ressemble à une option (« watch --ignore serve jardin » cible deux
# caméras, dont une nommée comme un verbe).
_OPTIONS_VALEURS_MULTIPLES = frozenset(("--exclude", "--include", "--ignore", "--unignore"))


def decouper_verbes(arguments) -> list:
    """Découpe « watch --loop download --loop merge » en groupes.

    Chaque nom de verbe connu ouvre un groupe, qui court jusqu'au verbe
    suivant : les options appartiennent donc au verbe qui les précède. C'est ce
    qui permet d'en citer autant qu'on veut, chacun réglé à sa façon.

    Un même verbe peut être cité plusieurs fois, avec des options différentes :
    « download --from usb --loop 10 download --from cloud --loop 1 » suit deux
    sources à deux cadences. La règle inverse, un verbe une seule fois, visait
    des réglages contradictoires ; ceux-là sont complémentaires.

    Lève ValueError si le premier élément n'est pas un verbe, faute de quoi on
    ne saurait pas à qui rattacher les premières options."""
    arguments = list(arguments)
    groupes = []
    indice, total = 0, len(arguments)
    while indice < total:
        element = arguments[indice]
        if element in VERBES:
            groupes.append([element])
            indice += 1
            continue
        if not groupes:
            raise ValueError(f"« {element} » n'est pas un verbe")
        groupes[-1].append(element)
        indice += 1
        if element in _OPTIONS_VALEUR_UNIQUE and indice < total:
            groupes[-1].append(arguments[indice])
            indice += 1
        elif element in _OPTIONS_VALEURS_MULTIPLES:
            while indice < total and not arguments[indice].startswith("-"):
                groupes[-1].append(arguments[indice])
                indice += 1
        elif (element == "--loop" and indice < total
              and arguments[indice].lstrip("-").isdigit()):
            # nargs="?" : --loop seul est valide (cadence par défaut), donc sa
            # valeur n'est consommée que si le mot suivant est bien un nombre.
            groupes[-1].append(arguments[indice])
            indice += 1
    return groupes


def cadence_positive(valeur: str) -> int:
    """Convertit une cadence CLI en entier strictement positif."""
    try:
        minutes = int(valeur)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "la cadence doit être un nombre entier de minutes"
        ) from exc
    if minutes <= 0:
        raise argparse.ArgumentTypeError(
            "la cadence doit être strictement supérieure à zéro"
        )
    return minutes


def jours_non_negatifs(valeur: str) -> int:
    """Convertit un nombre de jours CLI et autorise explicitement zéro."""
    try:
        jours = int(valeur)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "le nombre de jours doit être un entier"
        ) from exc
    if jours < 0:
        raise argparse.ArgumentTypeError(
            "le nombre de jours doit être positif ou nul"
        )
    return jours


def port_valide(valeur: str) -> int:
    """Convertit un port CLI et refuse les valeurs hors plage TCP/UDP."""
    try:
        port = int(valeur)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "le port doit être un nombre entier"
        ) from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(
            "le port doit être compris entre 1 et 65535"
        )
    return port


def ajouter_boucle(parser) -> None:
    """Ajoute --loop à un analyseur d'arguments.

    La répétition est une manière de travailler, pas un travail : elle
    s'applique donc à n'importe quel verbe plutôt que d'en constituer un. Écrite
    une seule fois ici, elle se comporte pareil partout."""
    parser.add_argument(
        "--loop", type=cadence_positive, nargs="?", const=10, default=None,
        metavar="MINUTES",
        help="répéter toutes les N minutes au lieu d'agir une fois (défaut 10)",
    )


def repeter(travail, minutes, journal=None) -> int:
    """Exécute `travail` une fois, ou indéfiniment si `minutes` est donné.

    Deux défauts de la boucle de fond sont corrigés ici, pour tous les verbes
    qui partagent cette fonction :

    - une erreur transitoire (réseau, ffmpeg, JSON) ne doit pas tuer
      définitivement le worker : seul Ctrl+C doit l'arrêter (I-17) ;
    - l'échéance du prochain tour se calcule depuis l'heure de départ du tour
      courant, sur time.monotonic(), plutôt que de dormir après le travail :
      un tour qui dure plus longtemps que la cadence ne doit pas décaler tous
      les suivants d'autant (O-05)."""
    import time

    if not minutes:
        return int(travail() or 0)
    print(f"Répétition toutes les {minutes} min. Ctrl+C pour arrêter.")
    if journal:
        journal(f"repetition toutes les {minutes} min")
    periode = minutes * 60
    try:
        while True:
            echeance = time.monotonic() + periode
            try:
                travail()
            except Exception as erreur:
                message = f"tour interrompu par une erreur, on reessaie au prochain : {erreur}"
                print(message)
                if journal:
                    journal(message)
            time.sleep(max(0.0, echeance - time.monotonic()))
    except KeyboardInterrupt:
        if journal:
            journal("arret de la repetition")
        print("\nArrêt.")
    return 0


def commande_composee(arguments) -> list:
    """Ligne complète passant par le point d'entrée, verbes compris.

    Distincte de self_command, qui vise le programme d'un seul verbe : ici on
    veut que blink lui-même reçoive la suite, puisque c'est lui qui sait lancer
    plusieurs verbes côte à côte."""
    if frozen():
        return [sys.executable, *arguments]
    return [sys.executable, "-u",
            str(Path(__file__).resolve().parent / "blink2video.py"), *arguments]


def self_command(verb: str, *arguments: str) -> list:
    """Ligne de commande pour se relancer sur un autre verbe.

    Le seul endroit du programme qui connaisse la différence entre les deux
    modes d'exécution. Tout le reste appelle self_command et ignore s'il tourne
    depuis des sources ou depuis un exécutable."""
    if frozen():
        return [sys.executable, verb, *arguments]
    if verb not in VERBES:
        raise ValueError(f"verbe inconnu : {verb}")
    module = VERBES[verb].module
    base = [sys.executable, "-u",
            str(Path(__file__).resolve().parent / f"{module}.py")]
    # Le point d'entrée attend son verbe comme premier argument positionnel ;
    # les autres programmes sont appelés directement, sans verbe.
    return base + ([verb] if module == ENTREE else []) + list(arguments)
