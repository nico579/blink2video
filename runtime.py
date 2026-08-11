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
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple


# Version de l'outil, et seule source de l'étiquette de publication : le
# workflow de release refuse une étiquette qui ne lui correspond pas. Un binaire
# doit pouvoir dire ce qu'il est, ne serait-ce que pour qu'un rapport de bogue
# soit exploitable.
VERSION = "0.2.1"


# Source unique des verbes : leur ordre d'apparition dans l'aide, le programme
# qui les traite, et ce qu'ils font. Trois usages en découlent, l'aide affichée,
# la délégation d'un verbe au bon programme, et la relance de l'outil sur un
# autre verbe. Les tenir à trois endroits garantissait qu'ils divergent, ce qui
# s'est produit : « autostart » manquait dans une description, « list » et
# « login » n'apparaissaient nulle part dans la liste des verbes.
#
# Le programme « blink » désigne blink.py lui-même, qui traite ces verbes sans
# déléguer : le verbe lui est alors passé en argument.
# Chaque verbe porte ses deux libellés : l'aide en ligne prend le français,
# docs.py prend l'un ou l'autre selon le README qu'il remplit. Le README anglais
# a longtemps affiché la liste des verbes en français, faute de cette colonne.
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
    "login": Verbe("blink",
              "se connecter au compte Blink, vérification en deux étapes gérée",
              "sign in to the Blink account, two-step verification handled"),
    "list": Verbe("blink",
             "ce que contient le module de synchronisation en ce moment",
             "what the Sync Module currently holds"),
    "download": Verbe("blink",
                 "récupérer les nouveaux clips avant que la rotation ne les efface",
                 "fetch new clips before rotation erases them"),
    "merge": Verbe("merge_daily",
              "normaliser, horodater et assembler jour, semaine et mois",
              "normalize, stamp and assemble day, week and month"),
    "watch": Verbe("watch",
              "contrôler l'état de l'installation et alerter s'il se dégrade",
              "check the installation and alert when it degrades"),
    "all": Verbe("daily",
            "tout, c'est-à-dire watch puis download puis merge",
            "everything, that is watch then download then merge"),
    "serve": Verbe("serve",
              "servir l'interface web, pour regarder, écarter, voir en direct",
              "serve the web interface, to watch, discard, see live"),
    "stop": Verbe("blink",
            "arrêter l'instance qui tourne en fond",
            "stop the instance running in the background"),
    "autostart": Verbe("autostart",
                  "inscrire à l'ouverture de session la commande qui suit",
                  "register the command that follows with your session"),
    "smoketest": Verbe("smoketest",
                  "vérifier que l'installation fonctionne sur cette machine",
                  "check that the installation works on this machine"),
}

# Verbes confiés à un autre programme, déduits de la table ci-dessus.
DELEGUES = {nom: verbe.module for nom, verbe in VERBES.items()
            if verbe.module != "blink"}


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
        return True
    resultat = lancer(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                      stderr=subprocess.DEVNULL, text=True, errors="replace",
                      check=False)
    return str(pid) in (resultat.stdout or "")


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
    """Fiches des instances réellement en cours, les périmées étant effacées."""
    dossier = app_dir() / INSTANCES
    vivantes = []
    for fiche in sorted(dossier.glob("*.json")) if dossier.is_dir() else []:
        try:
            donnees = json.loads(fiche.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            fiche.unlink(missing_ok=True)
            continue
        if processus_vivant(int(donnees.get("pid") or 0)):
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

    Sous Windows la question ne se pose pas : « taskkill /T » descend l'arbre
    d'un processus donné, sans toucher à ses frères ni à son parent."""
    if os.name == "nt":
        lancer(["taskkill", "/F", "/T", "/PID", str(pid)],
               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
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


def decouper_verbes(arguments) -> list:
    """Découpe « watch --loop download --loop merge » en groupes.

    Chaque nom de verbe connu ouvre un groupe, qui court jusqu'au verbe
    suivant : les options appartiennent donc au verbe qui les précède. C'est ce
    qui permet d'en citer autant qu'on veut, chacun réglé à sa façon.

    Lève ValueError si le premier élément n'est pas un verbe, faute de quoi on
    ne saurait pas à qui rattacher les premières options, et si un verbe est
    cité deux fois : deux réglages contradictoires pour la même chose seraient
    ambigus, et rien ne justifie de lancer deux fois le même travail."""
    groupes = []
    for element in arguments:
        if element in VERBES:
            if any(groupe[0] == element for groupe in groupes):
                raise ValueError(f"« {element} » est cité deux fois")
            groupes.append([element])
        elif groupes:
            groupes[-1].append(element)
        else:
            raise ValueError(f"« {element} » n'est pas un verbe")
    return groupes


def ajouter_boucle(parser) -> None:
    """Ajoute --loop à un analyseur d'arguments.

    La répétition est une manière de travailler, pas un travail : elle
    s'applique donc à n'importe quel verbe plutôt que d'en constituer un. Écrite
    une seule fois ici, elle se comporte pareil partout."""
    parser.add_argument(
        "--loop", type=int, nargs="?", const=10, default=None, metavar="MINUTES",
        help="répéter toutes les N minutes au lieu d'agir une fois (défaut 10)",
    )


def repeter(travail, minutes, journal=None) -> int:
    """Exécute `travail` une fois, ou indéfiniment si `minutes` est donné."""
    import time

    if not minutes:
        return int(travail() or 0)
    print(f"Répétition toutes les {minutes} min. Ctrl+C pour arrêter.")
    if journal:
        journal(f"repetition toutes les {minutes} min")
    try:
        while True:
            travail()
            time.sleep(minutes * 60)
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
            str(Path(__file__).resolve().parent / "blink.py"), *arguments]


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
    # blink.py attend son verbe comme premier argument positionnel ; les autres
    # programmes sont appelés directement, sans verbe.
    return base + ([verb] if module == "blink" else []) + list(arguments)
