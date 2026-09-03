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

from __future__ import annotations  # Python 3.8 (build Windows 7) : les annotations "int | None" ne s'évaluent qu'à l'écriture des chaînes, jamais à l'exécution.

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
VERSION = "0.11.3"
WINDOWS7_BUILD_MARKER = "windows7-build.txt"


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
REGLAGES_DEFAUT = {"usb_minutes": 10, "cloud_minutes": 1, "port": 8765, "timestamp": False,
                   "timezone": "Europe/Paris", "merge_jour": True, "merge_semaine": False,
                   "merge_mois": False, "download_auto": True, "live_protocol": "webrtc"}
# Remplace la variable d'environnement BLINK_DIRECT_WEBRTC (experimentale,
# BACKLOG.md 2026-09-03) une fois WebRTC valide en usage reel : un vrai
# reglage, pas juste une variable a poser avant de lancer le serveur. "mse"
# reste possible en repli volontaire (reglage cote page web) ; serve.py
# retombe de toute facon sur MSE si aiortc n'est pas installe, quel que
# soit ce choix. MJPEG deja compare a MSE une fois (audit 28.15) et son
# code mort retire depuis (commit 7339f85) : pas reintroduit comme
# troisieme option sans raison nouvelle.
PROTOCOLES_LIVE_VALIDES = ("webrtc", "mse")
# Hebdo et mensuel réencodaient par défaut la même matière que le quotidien
# (chaque assemblage ré-encode ses clips, jamais un simple regroupement de
# fichiers) : sur une installation neuve, ce triplement silencieux de
# l'espace et du temps CPU surprenait plus qu'il n'aidait. Quotidien reste
# seul par défaut ; les deux autres restent un choix explicite dans les
# réglages (signalé sur Reddit, 2026-08-26).
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
        "live_protocol": valeurs.get("live_protocol") if valeurs.get("live_protocol")
        in PROTOCOLES_LIVE_VALIDES else REGLAGES_DEFAUT["live_protocol"],
    }


def ecrire_reglages(usb_minutes: int, cloud_minutes: int, port: int, timestamp: bool,
                    timezone: str, merge_jour: bool, merge_semaine: bool,
                    merge_mois: bool, download_auto: bool, live_protocol: str) -> None:
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
                        "download_auto": bool(download_auto),
                        "live_protocol": str(live_protocol)}),
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
        # Un seul worker inventorie d'abord les deux sources : la barre connaît
        # ainsi le nombre global de clips à rapatrier, notamment juste après un
        # changement de dossier. Il conserve ensuite les deux cadences sans
        # réveiller l'USB aussi souvent que le cloud.
        download = ["download", "--from", "all",
                    "--usb-loop", str(c["usb_minutes"]),
                    "--cloud-loop", str(c["cloud_minutes"])]
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
    # blink_auth complète le magasin TLS système avec ces racines à jour.
    "certifi": "certifi",
    # Windows n'embarque aucune base de fuseaux horaires : sans ce paquet,
    # ZoneInfo("Europe/Paris") échoue et tout l'horodatage avec.
    "tzdata": "tzdata",
    # find_ffmpeg() (merge_daily.py) s'en sert par défaut, pour ne pas
    # dépendre d'un ffmpeg déjà présent sur la machine (revue du 27/08,
    # bug 4 : absent d'ici jusque-là, alors que requirements.in l'a
    # toujours listé).
    "imageio_ffmpeg": "imageio-ffmpeg",
}
if sys.version_info < (3, 9):
    # zoneinfo est stdlib depuis 3.9 ; en dessous (édition Windows 7,
    # Python 3.8), backports.zoneinfo le fournit - même condition que
    # requirements.in.
    DEPENDANCES["backports.zoneinfo"] = "backports.zoneinfo"


def extraire_mode_bootstrap(argv: list) -> list:
    """Retire --bootstrap=... de `argv` s'il y est, et reporte sa valeur dans
    la variable d'environnement BLINK_BOOTSTRAP. Renvoie `argv` sans lui.

    --bootstrap n'est déclaré par aucun parseur de verbe, seulement compris
    par bootstrap() : laissé dans argv, il fait échouer argparse sur
    "unrecognized arguments" avant même que bootstrap() ait pu le lire. À
    appeler donc AVANT tout parse_args(), pas seulement au sein de
    bootstrap() elle-même - route() (blink_cli.py) s'en sert tout en haut
    pour couvrir aussi les verbes comme download, dont les arguments
    n'étaient nettoyés par personne avant d'atteindre argparse (revue du
    27/08, bug 4)."""
    nettoye = []
    for argument in argv:
        if argument.startswith("--bootstrap="):
            os.environ["BLINK_BOOTSTRAP"] = argument.split("=", 1)[1]
        else:
            nettoye.append(argument)
    return nettoye


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

    sys.argv[1:] = extraire_mode_bootstrap(sys.argv[1:])
    mode = os.environ.get("BLINK_BOOTSTRAP", "auto")

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

    if not _venv_a_jour(python):
        _installer(str(python), list(DEPENDANCES.values()))

    print(f"Relance dans {venv_dir}...")
    os.execve(str(python), [str(python), *sys.argv],
              dict(os.environ, BLINK_BOOTSTRAP_DONE="1"))


def _venv_a_jour(python: Path) -> bool:
    """Vrai si l'interpréteur du venv importe déjà toutes les dépendances.

    Un venv créé par une version plus ancienne du programme peut avoir pris
    forme sans jamais recevoir un paquet ajouté depuis (ex. imageio-ffmpeg,
    revue du 27/08, bug 4) : sans cette vérification à chaque relance,
    _installer() n'était appelé qu'à la création, jamais pour réparer un
    venv déjà là mais incomplet. Interroger le venv lui-même (plutôt que de
    ne comparer qu'une liste) reste vrai même après une install manuelle ou
    un pip cassé dans ce venv précis."""
    verif = "import " + ", ".join(DEPENDANCES.keys())
    resultat = subprocess.run([str(python), "-c", verif],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              check=False)
    return resultat.returncode == 0


def _installer(python: str, paquets: list) -> None:
    print("Installation de : " + ", ".join(paquets))
    subprocess.run([python, "-m", "pip", "install", "--quiet", *paquets], check=True)


def frozen() -> bool:
    """Vrai lorsque le programme tourne depuis un bundle PyInstaller."""
    return bool(getattr(sys, "frozen", False))


POINTEUR_STOCKAGE = "blink_home.txt"
MARQUEUR_CONFIGURATION_INITIALE = ".blink_configuration_initiale_effectuee"
MARQUEUR_CONFIGURATION_EN_ATTENTE = ".blink_configuration_initiale_en_attente"


def _dossier_ancre() -> Path:
    """Emplacement par défaut, celui d'avant tout réglage : à côté de
    l'exécutable (figé) ou des sources. Fixe, jamais lui-même redirigé -
    c'est justement ce qui permet d'y chercher `POINTEUR_STOCKAGE` sans
    dépendre de la valeur qu'il contient (sinon, pour savoir où lire le
    réglage, il faudrait déjà connaître ce que le réglage doit dire)."""
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def app_dir_depuis(ancre: Path) -> Path:
    """Dossier de données qu'annoncerait app_dir(), mais à partir d'une ancre
    donnée plutôt que celle de ce processus (BLINK_HOME mis à part).

    Sert à maj.py : la version fraîchement extraite tourne depuis un dossier
    temporaire, dont l'ancre naturelle ne connaît pas `blink_home.txt` de
    l'installation réelle. Elle a besoin d'annoncer le bon dossier à la
    version relancée sans pour autant l'enfermer dans l'ancre elle-même si
    l'utilisateur a redirigé son stockage : suivre le pointeur depuis l'ancre
    fournie donne l'un ou l'autre correctement, contrairement à imposer
    l'ancre elle-même sans le lire (BLINK_HOME l'écraserait alors, y compris
    pour un stockage explicitement redirigé ailleurs - c'est ce qui, en
    pratique, ramenait le dossier de données à celui de l'exécutable après
    chaque mise à jour, signalé sur Reddit, 2026-08-26)."""
    pointeur = ancre / POINTEUR_STOCKAGE
    try:
        cible = pointeur.read_text(encoding="utf-8").strip()
    except OSError:
        cible = ""
    if cible:
        return Path(cible).expanduser().resolve()
    return ancre


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
    return app_dir_depuis(_dossier_ancre())


def ajouter_ligne(nom_fichier: str, ligne: str) -> None:
    """Ajoute une ligne à un fichier de app_dir(), append-only, jamais fatal.

    Pour les traces qui doivent survivre même sans console : le lancement
    normal se fait sous pythonw (autostart), qui n'a ni stdout ni stderr
    observables. watch.py a son propre journal() pour la même raison ;
    ceci en est l'équivalent partagé pour les autres appelants (le
    timestamp reste au format choisi par l'appelant, pas ajouté ici, pour
    ne pas le dupliquer avec un éventuel print() de la même ligne)."""
    try:
        with (app_dir() / nom_fichier).open("a", encoding="utf-8") as fichier:
            fichier.write(ligne + "\n")
    except OSError:
        pass


def lire_dossier_stockage() -> str:
    """Dossier de données effectif, tel qu'affiché dans le panneau de
    réglages : la valeur réelle (app_dir()), pas le contenu brut du
    pointeur, absent tant que personne n'a rien changé."""
    return str(app_dir())


def ecrire_dossier_stockage(chemin: str) -> None:
    """Enregistre le nouveau dossier, ou efface le réglage si `chemin` est
    vide (retour à l'emplacement par défaut).

    Ne déplace pas les clips : les fichiers déjà présents à l'ancien
    emplacement y restent, à charge de qui change ce réglage de les
    reprendre lui-même - choix délibéré, un déplacement automatique
    engageant bien plus (espace disque, fichiers ouverts, échec à
    mi-chemin) pour des données qui peuvent peser plusieurs Go.

    Les réglages et la session, eux, sont copiés vers le nouvel
    emplacement : ce n'est pas de la donnée gérée par l'application mais
    son état courant, et le nouveau app_dir() démarrerait sinon vide -
    déconnecté, réglages revenus aux défauts, y compris ceux tout juste
    enregistrés dans le même appel (ecrire_reglages() écrit dans l'ancien
    emplacement, juste avant celui-ci - revue du 27/08, "je perds mon
    authentification" en changeant de dossier). Une copie, jamais un
    déplacement : l'ancien emplacement reste utilisable si ce changement
    est annulé ensuite."""
    import uuid

    ancien = app_dir()
    ancre = _dossier_ancre()
    pointeur = ancre / POINTEUR_STOCKAGE
    chemin = chemin.strip()
    destination = (Path(chemin).expanduser().resolve() if chemin else ancre)

    # BLINK_HOME a priorité sur le pointeur. On mémorise tout de même le choix
    # demandé pour le jour où cette variable ne sera plus fournie, mais il ne
    # faut pas copier un fichier sur lui-même dans la racine actuellement
    # imposée par l'environnement.
    nouveau = ancien if os.environ.get("BLINK_HOME") else destination

    # Préparer intégralement la nouvelle racine AVANT de publier le pointeur.
    # Auparavant le pointeur changeait d'abord : une copie de session refusée
    # ou un disque retiré à cet instant laissait l'application basculée vers
    # un dossier incomplet. Ici, tout échec conserve l'ancienne racine active.
    if nouveau != ancien:
        nouveau.mkdir(parents=True, exist_ok=True)
        for nom in (REGLAGES, "blink_auth.json"):
            source = ancien / nom
            if not source.is_file():
                continue
            cible = nouveau / nom
            temporaire_copie = cible.with_name(
                f".{cible.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
            try:
                shutil.copy2(source, temporaire_copie)
                temporaire_copie.replace(cible)
            finally:
                temporaire_copie.unlink(missing_ok=True)

    if not chemin:
        # La suppression elle-même est le commit du retour à l'ancre : les
        # fichiers nécessaires y ont déjà été copiés juste au-dessus.
        pointeur.unlink(missing_ok=True)
        return

    # Écriture atomique du pointeur : un arrêt brutal ne peut plus laisser un
    # blink_home.txt vide ou tronqué que le prochain démarrage interpréterait
    # comme un retour silencieux à l'ancien stockage.
    temporaire_pointeur = pointeur.with_name(
        f".{pointeur.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        temporaire_pointeur.write_text(str(destination), encoding="utf-8")
        temporaire_pointeur.replace(pointeur)
    finally:
        temporaire_pointeur.unlink(missing_ok=True)


def resource_dir() -> Path:
    """Dossier des ressources embarquées dans le bundle."""
    if frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    return Path(__file__).resolve().parent


def build_windows7() -> bool:
    """Vrai uniquement dans le bundle legacy marqué au moment du build."""
    return frozen() and (resource_dir() / WINDOWS7_BUILD_MARKER).is_file()


def version_affichee() -> str:
    """Version numérique stable, complétée par la saveur du bundle legacy."""
    suffixe = " (Windows 7 experimental)" if build_windows7() else ""
    return VERSION + suffixe


def est_relatif_a(chemin: Path, racine: Path) -> bool:
    """Équivalent de Path.is_relative_to, absent avant Python 3.9 (build
    Windows 7, Python 3.8 : voir build-win7.yml)."""
    try:
        chemin.relative_to(racine)
    except ValueError:
        return False
    return True


# Une fiche par instance en cours, nommée d'après le processus qui la tient.
# Un fichier unique aurait suffi tant qu'une seule instance tourne, mais rien ne
# l'interdit : celle du démarrage automatique et une autre lancée à la main
# coexistent très bien, et « stop » doit pouvoir les arrêter toutes.
INSTANCES = Path(".blink_run")
INSTANCE_PID_ENV = "BLINK_INSTANCE_PID"


def _dossier_controle() -> Path:
    """Racine fixe des fichiers servant à piloter les processus en cours.

    Ces fichiers ne sont pas des données utilisateur. Les placer dans
    ``app_dir()`` les rendait invisibles exactement au moment où le dossier de
    stockage changeait : l'instance courante restait décrite dans l'ancien
    ``.blink_run``, tandis que ``stop`` la cherchait dans le nouveau. Sous
    Windows, le nouveau serveur échouait alors à reprendre le port et l'ancien
    continuait à montrer les clips de l'ancien dossier.

    L'ancre de l'installation, qui contient déjà le pointeur de stockage, ne
    change pas pendant ce basculement. ``BLINK_HOME`` reste respecté pour les
    installations dont l'emplacement du programme n'est pas inscriptible.
    """
    force = os.environ.get("BLINK_HOME")
    if force:
        return Path(force).expanduser().resolve()
    return _dossier_ancre()


def _ecrire_marqueur_configuration(nom: str) -> None:
    """Écrit atomiquement un petit marqueur dans la racine fixe de contrôle.

    Il ne vit volontairement pas dans ``app_dir()`` : changer le dossier de
    stockage vers une destination vide ne doit jamais être confondu avec une
    nouvelle installation. ``_dossier_controle()`` reste à côté de
    l'exécutable même lorsque ``blink_home.txt`` redirige les clips.
    """
    import uuid

    dossier = _dossier_controle()
    dossier.mkdir(parents=True, exist_ok=True)
    cible = dossier / nom
    temporaire = dossier / f".{nom}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        temporaire.write_text(VERSION, encoding="utf-8")
        temporaire.replace(cible)
    finally:
        temporaire.unlink(missing_ok=True)


def _traces_installation_existante() -> bool:
    """Détecte une installation antérieure dépourvue de notre marqueur.

    Le marqueur est nouveau ; sans migration, toute mise à jour ouvrirait le
    parcours initial à des utilisateurs déjà installés. Une session, des
    réglages, un pointeur de stockage ou un dossier de données non vide sont
    des preuves suffisantes d'un usage antérieur. Cette détection ne sert
    qu'une fois : elle est aussitôt matérialisée par le marqueur définitif.
    """
    racine = app_dir()
    if (racine / REGLAGES).is_file() or (racine / "blink_auth.json").is_file():
        return True
    if (_dossier_ancre() / POINTEUR_STOCKAGE).is_file():
        return True
    for nom in ("Blink_Clips", "Blink_Daily", "Blink_Weekly", "Blink_Monthly",
                "Blink_Normalized", "Blink_Excluded"):
        dossier = racine / nom
        try:
            if dossier.is_dir() and next(dossier.iterdir(), None) is not None:
                return True
        except OSError:
            # Une racine momentanément indisponible ne prouve ni une nouvelle
            # installation ni une ancienne. Les autres traces peuvent encore
            # trancher ; à défaut, le parcours initial reste le choix sûr.
            continue
    return False


def configuration_initiale_effectuee() -> bool:
    """Vrai après validation explicite du premier panneau de réglages."""
    return (_dossier_controle() / MARQUEUR_CONFIGURATION_INITIALE).is_file()


def configuration_initiale_requise() -> bool:
    """Vrai uniquement pour une installation réellement neuve.

    Un marqueur « en attente » est posé avant même la connexion Blink. Ainsi,
    fermer la fenêtre après s'être connecté mais avant d'avoir appliqué les
    réglages ne transforme pas cette session fraîche en fausse installation
    historique au lancement suivant.
    """
    if configuration_initiale_effectuee():
        return False
    attente = _dossier_controle() / MARQUEUR_CONFIGURATION_EN_ATTENTE
    if attente.is_file():
        return True
    if _traces_installation_existante():
        try:
            marquer_configuration_initiale()
        except OSError:
            # La migration est une optimisation de compatibilité. Les traces
            # resteront présentes au prochain démarrage et éviteront toujours
            # d'imposer le parcours initial, même si le marqueur est refusé.
            pass
        return False
    try:
        _ecrire_marqueur_configuration(MARQUEUR_CONFIGURATION_EN_ATTENTE)
    except OSError:
        # Ne jamais lancer les téléchargements pour la seule raison qu'un
        # marqueur n'a pas pu s'écrire : le panneau permettra d'expliquer le
        # vrai problème de permissions lors de l'enregistrement.
        pass
    return True


def marquer_configuration_initiale() -> None:
    """Valide définitivement le parcours initial, indépendamment du stockage."""
    _ecrire_marqueur_configuration(MARQUEUR_CONFIGURATION_INITIALE)
    try:
        (_dossier_controle() / MARQUEUR_CONFIGURATION_EN_ATTENTE).unlink(missing_ok=True)
    except OSError:
        # Le marqueur définitif est le commit et il est testé en premier. Un
        # antivirus qui tient encore l'ancien petit fichier ne doit donc pas
        # transformer une validation réussie en erreur ni bloquer le démarrage.
        pass


def identite_processus(pid: int) -> str | None:
    """Identité stable d'un processus vivant : sa vraie date de démarrage
    aupres de l'OS, jamais réattribuée contrairement au pid seul.

    Un verrou ou une marque d'avancement (AUDIT-2026-08-13, 28.82/28.84)
    n'enregistrait que le pid de son propriétaire : après un redémarrage,
    Windows réattribue vite les numéros de pid à d'autres processus, sans
    rapport avec le nôtre. `processus_vivant(pid)` répondait alors « oui »
    à tort, pour toujours (aucun garde-fou sur l'âge, volontairement, pour
    ne jamais voler le verrou d'un processus réellement vivant) : le verrou
    restait bloqué indéfiniment, tenu par un fantôme. Comparer cette
    identité à l'ouverture puis à chaque relecture distingue « même
    processus, toujours vivant » de « pid recyclé par quelqu'un d'autre »."""
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            # ctypes suppose sinon qu'une fonction C renvoie un int 32 bits ;
            # un HANDLE 64 bits pourrait être tronqué sur Windows x64.
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = (
                wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME))
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return None
            try:
                creation, exit_t, kernel_t, user_t = (
                    wintypes.FILETIME(), wintypes.FILETIME(),
                    wintypes.FILETIME(), wintypes.FILETIME(),
                )
                ok = kernel32.GetProcessTimes(
                    handle, ctypes.byref(creation), ctypes.byref(exit_t),
                    ctypes.byref(kernel_t), ctypes.byref(user_t))
                if not ok:
                    return None
                return f"{creation.dwLowDateTime}:{creation.dwHighDateTime}"
            finally:
                kernel32.CloseHandle(handle)
        except OSError:
            return None
    try:
        resultat = lancer(["ps", "-o", "lstart=", "-p", str(pid)],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, errors="replace",
                          check=False)
    except OSError:
        # `ps` absent (constate en reel : l'image python:3.12-slim ne
        # l'installe pas par defaut, contrairement a une distribution
        # complete) : aucune comparaison possible, pas une raison de planter
        # l'appli entiere. `verrou()` retombe alors sur l'ancien
        # comportement (identite None = rien a comparer), moins protege
        # contre un pid recycle mais fonctionnel.
        return None
    valeur = (resultat.stdout or "").strip()
    return valeur or None


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
        try:
            etat = lancer(["ps", "-o", "state=", "-p", str(pid)],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, errors="replace",
                          check=False)
        except OSError:
            # `ps` absent (ex. python:3.12-slim, constate en reel via
            # identite_processus - AUDIT-2026-08-13, 28.85) : le signal 0 a
            # deja confirme que le pid existe, on ne peut juste pas trancher
            # zombie ou non. On le traite comme vivant, comme avant l'ajout
            # de cette verification.
            return True
        return not (etat.stdout or "").strip().startswith("Z")
    # Interroge l'OS directement via l'API Win32 (OpenProcess +
    # GetExitCodeProcess), sans passer par un sous-processus tasklist : un
    # lancement de processus coûte des dizaines à centaines de ms, ce qui
    # s'accumule vite pendant l'attente d'arrêt (arreter() vérifie plusieurs
    # membres chaque seconde) et rend le délai dépendant de la charge de la
    # machine plutôt que de l'état réel des processus. Même API que
    # identite_processus() ci-dessus.
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_ACCESS_DENIED = 5
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetLastError.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            erreur = kernel32.GetLastError()
            if erreur == ERROR_ACCESS_DENIED:
                # Le pid existe (sinon l'erreur serait "paramètre invalide"),
                # mais son ouverture nous est refusée : ça ne prouve rien sur
                # son état, seulement que la question n'a pas pu être posée.
                # Trancher "mort" ici volerait le verrou d'un propriétaire
                # pourtant vivant (même principe qu'avec tasklist avant lui).
                return True
            return False
        try:
            code_sortie = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code_sortie))
        finally:
            kernel32.CloseHandle(handle)
        return (code_sortie.value == STILL_ACTIVE) if ok else True
    except OSError:
        # kernel32 est le coeur de l'OS : ceci ne devrait jamais se produire,
        # mais si ça arrivait, même principe que la branche POSIX ci-dessus.
        return True


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
    # Les nouvelles fiches n'empruntent plus ce chemin : elles comparent
    # l'heure de création native via GetProcessTimes. Il reste uniquement pour
    # lire les fiches de l'ancienne version. Windows 7 est livré avec
    # PowerShell 2, sans Get-CimInstance (apparu en PowerShell 3) : choisir
    # dynamiquement Get-WmiObject sur cet ancien environnement évite que la
    # migration conclue « processus absent » faute de cmdlet.
    resultat = lancer(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         f"if (Get-Command Get-CimInstance -ErrorAction SilentlyContinue) {{ "
         f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' "
         f"-ErrorAction SilentlyContinue).CommandLine }} else {{ "
         f"(Get-WmiObject Win32_Process -Filter 'ProcessId={pid}' "
         f"-ErrorAction SilentlyContinue).CommandLine }}"],
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


def processus_correspond(pid: int, marqueurs: list | None = None,
                         identite: str | None = None) -> bool:
    """Vrai si ce PID désigne bien un processus de ce projet.

    Un numéro de processus fini par être réattribué à un logiciel sans
    aucun rapport ; le confondre avec l'instance qu'on croit suivre a déjà
    fait « arrêter » un service HP, un terminal et une messagerie sur la
    machine d'un utilisateur, simplement parce qu'un numéro coïncidait.
    Vérifier l'existence du PID ne suffit pas : il faut vérifier son
    identité avant d'y toucher, *a fortiori* avant de le tuer."""
    if not processus_vivant(pid):
        return False
    if identite is not None:
        # Pour toute fiche au nouveau format, PID + heure de création donnée
        # par l'OS est l'identité. Aucun PowerShell/WMI, donc même comportement
        # sur Windows 7 d'origine et sur Windows actuel. Si l'OS refuse de
        # répondre, ne jamais tuer au jugé : False est le choix sûr.
        actuelle = identite_processus(pid)
        return actuelle is not None and actuelle == identite
    ligne = ligne_de_commande(pid)
    return any(m in ligne for m in (marqueurs or _empreintes_attendues()))


def _ecrire_fiche(fiche: Path, donnees: dict) -> None:
    """Écrit une fiche de processus (instance ou travailleurs), atomiquement.

    Une écriture directe pouvait laisser une lecture concurrente voir du
    JSON tronqué en cours d'écriture ; lire_instances() traitait alors ça
    comme une fiche périmée et la supprimait, faisant perdre à stop la
    trace d'un processus pourtant vivant (revue du 27/08, bug 8). Même
    motif que _ecrire_registre() (blink_registre.py)."""
    import uuid

    temporaire = fiche.with_name(
        f".{fiche.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        temporaire.write_text(json.dumps(donnees, ensure_ascii=False), encoding="utf-8")
        temporaire.replace(fiche)
    finally:
        temporaire.unlink(missing_ok=True)


def _verrou_fiche(fiche: Path, attente: int = 5):
    """Sérialise les mises à jour parent/enfant d'une même fiche d'instance."""
    return verrou(f"instance-{fiche.stem}", f"fiche-{os.getpid()}",
                   stale_after=120, attente=attente, racine=_dossier_controle())


def inscrire_instance(entrees: list, enfants=None) -> Path:
    """Dépose la fiche de l'instance courante, et la retire à sa mort.

    Sans elle, arrêter une instance lancée sans console reviendrait à chercher
    des numéros de processus à la main, puis à tuer un arbre en espérant
    n'oublier personne. Purge aussi au passage les fiches d'instances mortes
    sans passage par « stop »."""
    import atexit
    import datetime as dt

    # Écrase aussi une valeur héritée d'un éventuel ancien superviseur lors
    # d'un redémarrage. Tous les enfants lancés ensuite sauront ainsi quelle
    # fiche enrichir lorsqu'ils inscrivent un ffmpeg.
    os.environ[INSTANCE_PID_ENV] = str(os.getpid())

    # Purge les fiches mortes sans passage par « stop » (kill externe,
    # plantage, coupure de courant) : lire_instances() les nettoie déjà comme
    # effet de bord de sa lecture, mais rien ne le lui demandait plus une fois
    # une session lancée, qui peut ensuite tourner des jours sans jamais
    # rappeler cette fonction (constaté en réel, 2026-09-01 : fiches vieilles
    # de plusieurs jours toujours dans .blink_run). Chaque lancement est
    # l'occasion de balayer les restes du précédent.
    instances_vivantes = lire_instances(journal=print)

    # Ne jamais effacer la demande d'un arrêt réellement en cours. Une nouvelle
    # instance lancée au même instant annulait sinon le signal sous les pieds
    # des anciennes boucles. Seul un drapeau orphelin, sans aucune instance
    # encore reconnue, appartient forcément à une session précédente.
    if arret_demande():
        if instances_vivantes:
            raise BusyError("arrêt en cours")
        effacer_arret_demande()

    dossier = _dossier_controle() / INSTANCES
    dossier.mkdir(parents=True, exist_ok=True)
    fiche = dossier / f"{os.getpid()}.json"
    membres = [os.getpid(), *(enfants or [])]
    identites = {}
    for pid in membres:
        identite = identite_processus(int(pid))
        if identite is not None:
            identites[str(pid)] = identite

    # Le second appel, juste après le lancement des enfants, complète la même
    # fiche. Le verrou ferme la course avec un enfant très rapide qui inscrirait
    # déjà ffmpeg : sans lui, les deux read-modify-replace pouvaient s'écraser.
    with _verrou_fiche(fiche):
        travailleurs = []
        try:
            precedente = json.loads(fiche.read_text(encoding="utf-8"))
            travailleurs = list(precedente.get("travailleurs") or [])
            anciennes_identites = precedente.get("identites") or {}
            if isinstance(anciennes_identites, dict):
                anciennes_identites = dict(anciennes_identites)
                anciennes_identites.update(identites)
                identites = anciennes_identites
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

        _ecrire_fiche(fiche, {
            "pid": os.getpid(),
            "depuis": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "verbes": entrees,
            # Les enfants sont notés pour pouvoir vérifier, après l'arrêt, qu'aucun
            # n'a survécu : c'est précisément ce qui se produisait avant.
            "enfants": list(enfants or []),
            "travailleurs": travailleurs,
            "identites": identites,
        })
    def nettoyer_fiche() -> None:
        """Retire la fiche seulement si aucun enfant suivi ne lui survit."""
        try:
            donnees = json.loads(fiche.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        identites_lues = donnees.get("identites") or {}
        if not isinstance(identites_lues, dict):
            identites_lues = {}
        autres = [*(donnees.get("enfants") or []),
                  *(donnees.get("travailleurs") or [])]
        for autre in autres:
            pid = int(autre or 0)
            attendue = identites_lues.get(str(pid))
            if attendue is not None:
                actuelle = identite_processus(pid)
                if actuelle is None and processus_vivant(pid):
                    return
                if actuelle == attendue:
                    return
            elif processus_correspond(pid):
                return
        fiche.unlink(missing_ok=True)

    atexit.register(nettoyer_fiche)
    return fiche


def lire_instances(journal=None) -> list:
    """Fiches des instances réellement en cours, les périmées étant effacées.

    I-13 : un parent mort ne suffit pas à dire l'instance terminée. Un enfant
    persistant peut lui survivre (le parent supervise, mais ne fait pas
    partie du même groupe de processus que ses enfants sous POSIX), et
    « stop » a besoin de la fiche pour retrouver ce survivant. On n'efface
    donc que les fiches dont ni le parent, ni aucun enfant, ne vivent plus.

    L'identité (PID + heure de création native, voir `identite_processus`)
    décide seule qui est encore « nous » : plus fort qu'un rapprochement sur
    la ligne de commande, déjà responsable d'avoir arrêté par erreur un
    service HP, un terminal et une messagerie sur la machine d'un utilisateur
    (voir `processus_correspond`). `journal`, optionnel, reçoit une ligne
    lisible (PID et verbes d'origine) pour chaque fiche périmée retirée : de
    quoi vérifier après coup *quelle* instance a été jugée morte, sans pour
    autant fonder la décision elle-même sur ce texte."""
    dossier = _dossier_controle() / INSTANCES
    vivantes = []
    for fiche in sorted(dossier.glob("*.json")) if dossier.is_dir() else []:
        try:
            donnees = json.loads(fiche.read_text(encoding="utf-8"))
        except OSError:
            # Disparue entre le glob() et la lecture : un autre processus
            # (son propre atexit, par exemple) l'a déjà nettoyée, rien à
            # purger ici.
            continue
        except json.JSONDecodeError:
            # Présente mais illisible. _ecrire_fiche() étant atomique
            # (temp + replace), ce n'est plus une fenêtre d'écriture
            # concurrente qu'on pourrait croiser ici, seulement une vraie
            # corruption : la supprimer ferait perdre à stop la trace d'un
            # processus peut-être toujours vivant (revue du 27/08, bug 8).
            # On la laisse plutôt en l'état pour cette lecture.
            continue
        membres = [donnees.get("pid"), *(donnees.get("enfants") or []),
                   *(donnees.get("travailleurs") or [])]
        identites = donnees.get("identites") or {}
        if not isinstance(identites, dict):
            identites = {}

        def encore_notre_processus(membre) -> bool:
            pid = int(membre or 0)
            attendue = identites.get(str(pid))
            if attendue is None:
                # Ancienne fiche : repli temporaire sur la ligne de commande.
                return processus_correspond(pid)
            actuelle = identite_processus(pid)
            if actuelle is None:
                # Distinguer « mort » de « vivant mais impossible à interroger ».
                # Dans le second cas, conserver la fiche : la supprimer ferait
                # perdre la seule piste sans avoir prouvé qu'elle est périmée.
                return processus_vivant(pid)
            return actuelle == attendue

        if any(encore_notre_processus(membre) for membre in membres):
            donnees["fiche"] = fiche
            vivantes.append(donnees)
        else:
            if journal:
                commande = " ".join(" ".join(g) for g in donnees.get("verbes") or [])
                journal(f"fiche perimee retiree : PID {donnees.get('pid')} "
                        f"({commande or '?'}), depuis {donnees.get('depuis', '?')}")
            fiche.unlink(missing_ok=True)
    return vivantes


def _fiche_courante() -> Path:
    # Les verbes tournent dans des processus enfants du superviseur qui porte
    # la fiche. Celui-ci transmet son PID par l'environnement : sans cela un
    # merge enfant cherchait « <son propre pid>.json », inexistant, et ffmpeg
    # n'était jamais réellement inscrit malgré le code prévu à cet effet.
    proprietaire = os.environ.get(INSTANCE_PID_ENV) or str(os.getpid())
    return _dossier_controle() / INSTANCES / f"{proprietaire}.json"


def inscrire_travailleur(pid: int) -> None:
    """Ajoute un PID de travailleur (ffmpeg en cours de fusion) à la fiche du
    processus courant.

    Un ffmpeg lancé en fusion est un vrai enfant de ce processus, mais
    `arreter()` ne lui applique jamais taskkill /T sur son propre PID (voir
    arreter_processus) : cette protection, ajoutée pour ne pas emporter un
    navigateur adopté par erreur, laissait aussi ffmpeg orphelin et tournant
    jusqu'à sa fin après un « stop ». L'inscrire ici lui donne une entrée que
    « stop » sait tuer directement, sans dépendre du taskkill du parent.

    Silencieux si la fiche n'existe pas encore : un appel direct aux
    fonctions de fusion hors supervision de blink_cli (tests, script) n'a
    simplement rien à enregistrer."""
    fiche = _fiche_courante()
    try:
        with _verrou_fiche(fiche):
            try:
                donnees = json.loads(fiche.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            travailleurs = set(donnees.get("travailleurs") or [])
            travailleurs.add(pid)
            donnees["travailleurs"] = sorted(travailleurs)
            identite = identite_processus(pid)
            if identite is not None:
                identites = dict(donnees.get("identites") or {})
                identites[str(pid)] = identite
                donnees["identites"] = identites
            _ecrire_fiche(fiche, donnees)
    except BusyError:
        # L'inscription est best-effort : le kill /T du processus merge reste
        # le repli si sa fiche est exceptionnellement restée occupée 5 secondes.
        return


def retirer_travailleur(pid: int) -> None:
    """Symétrique d'inscrire_travailleur : à appeler que le lot ait réussi,
    échoué, ou ait été tué par le chien de garde silence de run_ffmpeg_batch."""
    fiche = _fiche_courante()
    try:
        with _verrou_fiche(fiche):
            try:
                donnees = json.loads(fiche.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            travailleurs = set(donnees.get("travailleurs") or [])
            travailleurs.discard(pid)
            donnees["travailleurs"] = sorted(travailleurs)
            identites = dict(donnees.get("identites") or {})
            identites.pop(str(pid), None)
            donnees["identites"] = identites
            _ecrire_fiche(fiche, donnees)
    except BusyError:
        return


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


def _supprimer_verrou(fichier: Path) -> None:
    """Supprime un fichier de verrou qu'on sait nous appartenir, en absorbant
    un refus Windows transitoire.

    Un antivirus ou l'indexeur peut brièvement rouvrir un fichier qui vient
    d'être lu ou écrit ; sur Windows, ça suffit à faire échouer unlink() avec
    PermissionError (WinError 32) alors que plus aucun propriétaire légitime
    ne le détient. Quelques tentatives rapprochées suffisent, l'ouverture
    concurrente n'étant jamais longue."""
    for tentative in range(10):
        try:
            fichier.unlink(missing_ok=True)
            return
        except PermissionError:
            if tentative == 9:
                raise
            time.sleep(0.05)


@contextlib.contextmanager
def verrou(nom: str, owner: str, stale_after: int = 600, attente: int = 0,
           racine: Path | None = None):
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

    Le cas d'un PID recyclé par un processus non lié pendant que le vrai
    propriétaire est mort - longtemps documenté comme non couvert ici, et
    constaté en réel (AUDIT-2026-08-13, 28.82/28.84 : la boucle merge est
    restée bloquée plus de 15h après un redémarrage Windows qui a dû
    réattribuer le pid) - est désormais tranché via `identite_processus()` :
    la date de démarrage réelle du processus, donnée par l'OS, est comparée
    à celle enregistrée dans le verrou. Un pid vivant dont l'identité ne
    correspond plus n'est pas le même processus : le verrou est traité comme
    abandonné, purgé comme n'importe quelle marque périmée.

    `attente` donne le temps pendant lequel on réessaie avant de renoncer. Un
    direct dure quelques minutes ; sans attente, une boucle qui tombe dessus
    perdrait son tour entier, alors que la ressource se libère souvent en
    quelques secondes."""
    import uuid

    dossier_verrou = Path(racine) if racine is not None else app_dir()
    dossier_verrou.mkdir(parents=True, exist_ok=True)
    fichier = dossier_verrou / f".blink_{nom}.lock"
    jeton = uuid.uuid4().hex
    limite = time.time() + max(attente, 0)
    contenu = json.dumps(
        {"owner": owner, "pid": os.getpid(), "jeton": jeton, "at": time.time(),
         "identite": identite_processus(os.getpid())}
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
        pid_verrou = int(presente.get("pid") or 0)
        identite_enregistree = presente.get("identite")
        identite_actuelle = identite_processus(pid_verrou)
        # Vivant ET la même identité : un pid recyclé par un autre processus
        # après la mort du vrai propriétaire ne doit pas passer pour lui
        # (AUDIT-2026-08-13, 28.82/28.84). Une marque sans "identite" vient
        # d'un ancien format (avant ce correctif) : on ne peut alors rien
        # comparer, on retombe sur l'ancien comportement plutôt que de purger
        # à tort un verrou légitime. Même logique si identite_processus()
        # échoue à interroger le pid actuel (OpenProcess refusé, par exemple) :
        # un None ici ne prouve pas une identité différente, seulement qu'on
        # n'a pas pu la lire, donc on ne tranche pas "différent" sur cette
        # seule base (revue du 27/08, bug 1).
        meme_processus = (
            identite_enregistree is None
            or identite_actuelle is None
            or identite_actuelle == identite_enregistree
        )
        if not (processus_vivant(pid_verrou) and meme_processus):
            # Propriétaire mort, ou pid recyclé par quelqu'un d'autre : purge
            # sous mutex, pas par un unlink direct.
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
                    _supprimer_verrou(fichier)
            finally:
                _supprimer_verrou(purge)
            continue

        if time.time() >= limite:
            age = time.time() - float(presente.get("at") or 0)
            raise BusyError(f"déjà réservé par « {presente.get('owner')} » "
                            f"(pid {presente.get('pid')}) depuis {int(age)} s")
        time.sleep(1)

    try:
        yield
    finally:
        courante = _lire_verrou(fichier)
        if courante is not None and courante.get("jeton") == jeton:
            _supprimer_verrou(fichier)


def verrou_controle(owner: str, attente: int = 0):
    """Verrou global des transitions stop/restart, indépendant du stockage."""
    return verrou("controle", owner, stale_after=120, attente=attente,
                   racine=_dossier_controle())


def verrou_configuration(owner: str = "reglages", attente: int = 0):
    """Sérialise les bascules du pointeur et l'écriture de leurs réglages."""
    return verrou("configuration", owner, stale_after=120, attente=attente,
                   racine=_dossier_controle())


# Identité applicative sous laquelle les notifications Windows sont émises.
# Windows jette en silence, en rendant zéro, toute notification dont
# l'identité n'est pas déclarée : emprunter celle de PowerShell marchait sur
# certaines installations et pas sur d'autres, sans jamais dire pourquoi. On
# déclare donc la nôtre, comme le font Firefox ou Acrobat.
APP_ID = "blink2video"


ARRET_DEMANDE = Path(".blink_arret")


def demander_arret() -> None:
    """Pose le drapeau d'arrêt coopératif.

    repeter() (tous les verbes --loop) et le serveur web le relisent à
    chaque tour/à intervalle court : un verbe termine son tour en cours
    (fichier entamé, page en cours de service) puis s'arrête de lui-même,
    plutôt que d'être tué en plein milieu (revue du 27/08 : un arrêt externe,
    tray ou stop, tuait directement sans laisser le travail en cours se
    terminer proprement). Seul ffmpeg reste tué directement : processus
    tiers, on n'a aucune prise sur son propre nettoyage (voir arreter(),
    blink_cli.py)."""
    (_dossier_controle() / ARRET_DEMANDE).write_text(
        str(time.time()), encoding="utf-8")


def arret_demande() -> bool:
    return (_dossier_controle() / ARRET_DEMANDE).exists()


def effacer_arret_demande() -> None:
    """À appeler au début d'une nouvelle session (le drapeau d'une session
    précédente ne doit jamais empêcher une nouvelle de tourner) et à la fin
    d'une séquence d'arrêt réussie."""
    (_dossier_controle() / ARRET_DEMANDE).unlink(missing_ok=True)


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
TRAVAIL_TERMINE_VISIBLE = 10


def _fichier_travail(pid: int | None = None) -> Path:
    """Fiche propre à un worker, pour que deux progressions ne s'écrasent pas."""
    pid = os.getpid() if pid is None else int(pid)
    return app_dir() / f"{TRAVAIL.stem}.{pid}{TRAVAIL.suffix}"


def _ecrire_fiche_travail(cible: Path, etat: dict) -> bool:
    """Remplace une fiche atomiquement, y compris sous antivirus Windows."""
    import uuid

    temporaire = cible.with_name(
        f".{cible.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    reussi = False
    try:
        temporaire.write_text(json.dumps(etat, ensure_ascii=False), encoding="utf-8")
        temporaire.replace(cible)
        reussi = True
    except OSError:
        pass
    finally:
        try:
            temporaire.unlink(missing_ok=True)
        except OSError:
            pass
    return reussi


def _lire_fiche_travail(cible: Path) -> dict:
    try:
        etat = json.loads(cible.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return etat if isinstance(etat, dict) else {}


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
    # Une fiche par PID : download et merge peuvent réellement se chevaucher.
    # Un fichier unique faisait disparaître la barre de téléchargement dès que
    # l'assemblage publiait son propre tick, et fin_travail() de l'un pouvait
    # supprimer la publication fraîche de l'autre entre sa lecture et unlink.
    _ecrire_fiche_travail(_fichier_travail(), etat)


def fin_travail(conserver: float = 0) -> None:
    """Retire notre fiche, ou conserve brièvement son état final affichable.

    Une fin conservée porte ``termine`` : elle n'est donc jamais rendue par
    :func:`travail_en_cours` et ne bloque ni le bouton Actualiser ni un nouveau
    calcul. Elle permet seulement à la sonde web de ne pas manquer un petit
    téléchargement entièrement terminé entre deux interrogations.
    """
    import datetime as dt

    cible = _fichier_travail()
    etat = _lire_fiche_travail(cible)
    if not etat or etat.get("pid") != os.getpid():
        return
    total = etat.get("total") or 0
    fait = etat.get("fait") or 0
    if conserver and total > 0 and fait >= total:
        etat["termine"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        etat["visible_secondes"] = max(0, float(conserver))
        if _ecrire_fiche_travail(cible, etat):
            return
        # Sous Windows, un antivirus peut exceptionnellement refuser le
        # replace. Ne surtout pas laisser alors l'ancienne fiche « active »
        # pendant que le worker dort : mieux vaut perdre le N/N terminal que
        # bloquer Actualiser jusqu'à la péremption de quinze minutes.
    try:
        cible.unlink(missing_ok=True)
    except OSError:
        pass


def _etats_travail() -> tuple:
    """Renvoie (actifs, terminés encore affichables), en purgeant les périmés."""
    import datetime as dt

    maintenant = dt.datetime.now().astimezone()
    racine = app_dir()
    fichiers = list(racine.glob(f"{TRAVAIL.stem}.*{TRAVAIL.suffix}"))
    # Compatibilité avec une fiche laissée par une version antérieure pendant
    # une mise à jour en place. Les nouvelles écritures utilisent toutes le PID.
    ancienne = racine / TRAVAIL
    if ancienne.exists():
        fichiers.append(ancienne)

    actifs, termines = [], []
    for fichier in fichiers:
        etat = _lire_fiche_travail(fichier)
        perime = not etat
        termine = etat.get("termine") if etat else None
        if termine:
            try:
                fini = dt.datetime.fromisoformat(str(termine))
                duree = float(etat.get("visible_secondes", TRAVAIL_TERMINE_VISIBLE))
                perime = (maintenant - fini).total_seconds() > duree
            except (TypeError, ValueError):
                perime = True
            if not perime:
                termines.append(etat)
        elif etat:
            try:
                depuis = dt.datetime.fromisoformat(str(etat.get("depuis")))
                age = (maintenant - depuis).total_seconds()
                pid = int(etat.get("pid") or 0)
            except (TypeError, ValueError):
                perime = True
            else:
                perime = age > 900 or not processus_vivant(pid)
            if not perime:
                actifs.append(etat)
        if perime:
            try:
                fichier.unlink(missing_ok=True)
            except OSError:
                pass
    return actifs, termines


def _priorite_travail(etat: dict, actif: bool) -> tuple:
    """Le téléchargement reste visible face à un assemblage concurrent."""
    cle = str(etat.get("cle") or "")
    telechargement = cle in ("phase.inventory_clips", "phase.download_clips")
    # actif download > fin download > actif autre > fin autre
    categorie = (4 if actif else 3) if telechargement else (2 if actif else 1)
    return categorie, str(etat.get("termine") or etat.get("depuis") or "")


def travail_en_cours() -> dict:
    """Le travail en cours, ou un dictionnaire vide.

    Mêmes garde-fous que les verrous : une marque laissée par un processus mort
    ou trop vieille ne doit pas faire croire à un calcul éternel."""
    actifs, _ = _etats_travail()
    return max(actifs, key=lambda etat: _priorite_travail(etat, True)) if actifs else {}


def travail_affichable() -> dict:
    """Travail actif, ou dernière fin N/N retenue pour la prochaine sonde web."""
    actifs, termines = _etats_travail()
    candidats = [(etat, True) for etat in actifs] + [
        (etat, False) for etat in termines
    ]
    if not candidats:
        return {}
    choisi = dict(max(candidats, key=lambda item: _priorite_travail(*item))[0])
    # Le travail montré peut être un N/N terminal prioritaire alors qu'un
    # assemblage tourne encore derrière. Le navigateur doit distinguer
    # « visible » de « actif » pour ne pas réactiver son bouton à tort.
    choisi["actif"] = bool(actifs)
    return choisi


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
    "--crf", "--thumbs", "--usb-loop", "--cloud-loop",
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
        while not arret_demande():
            echeance = time.monotonic() + periode
            try:
                travail()
            except Exception as erreur:
                message = f"tour interrompu par une erreur, on reessaie au prochain : {erreur}"
                print(message)
                if journal:
                    journal(message)
            # Sommeil scindé en tranches courtes : sans ça, un arrêt demandé
            # pendant l'attente entre deux tours ne serait vu qu'à l'échéance
            # complète (jusqu'à `minutes` d'attente), pas dans la seconde
            # (revue du 27/08 : arrêt coopératif plutôt qu'un kill externe).
            while not arret_demande():
                reste = echeance - time.monotonic()
                if reste <= 0:
                    break
                time.sleep(min(1.0, reste))
    except KeyboardInterrupt:
        if journal:
            journal("arret de la repetition")
        print("\nArrêt.")
    else:
        if journal:
            journal("arret demande, sortie propre")
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
