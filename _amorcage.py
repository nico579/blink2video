"""Amorçage des dépendances d'une application lancée depuis ses sources.

Une application distribuée en code source (``python app.py``) doit, au premier
lancement, trouver ses bibliothèques : créer un environnement isolé (venv), y
installer le verrou ``requirements.txt`` (versions exactes, empreintes SHA-256
vérifiées par pip), puis s'y relancer. Les quatre applications (blink2video,
lidar2map, gpxsolar, watch2notif) en avaient chacune une version, qui avait
dérivé : mêmes fonctions de lecture du verrou, deux moteurs de venv différents,
une troisième installation sans verrou.

Ce module est LA version commune, avec une contrainte qui le distingue des
autres : il tourne AVANT que ses propres dépendances (dont ce paquet) soient
installées. Il ne peut donc pas être importé depuis ``nico579_commons`` au
démarrage. Chaque application en garde une copie octet pour octet
(``_amorcage.py``, à côté de son point d'entrée), que la même suite de tests
compare à celle du paquet installé : toute dérive fait échouer la CI. C'est le
procédé de pip et de setuptools pour leurs dépendances « vendorisées ».

Bibliothèque standard seule, Python 3.8 compris. Aucun effet à l'import.

Quatre modes, choisis par ``--bootstrap=<mode>`` ou ``<NOM>_BOOTSTRAP`` :

``auto``   (défaut) un venv ``~/.<nom>/venv``, créé au premier lancement, remis
           au verrou quand celui-ci change, où le programme se relance ;
``force``  comme ``auto``, même si un autre environnement est déjà actif ;
``pip``    installation du verrou dans l'environnement Python courant ;
``none``   rien n'est installé : on vérifie et on explique ce qui manque.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

MODES = ("auto", "pip", "none", "force")
OPTION_INSTALLER = "--installer-deps"
# Anciens raccourcis de lidar2map, conservés : leur priorité est fixe
# (--no-venv l'emporte sur --venv, qui l'emporte sur --no-bootstrap).
ALIAS = (("--no-bootstrap", "none"), ("--venv", "auto"), ("--no-venv", "pip"))
DELAI_PIP_S = 1800

LIBELLES = {
    "en": {
        "aide": (
            "Dependencies when running {nom} from its sources.\n\n"
            "  --bootstrap=auto    (default) isolated venv in {dossier}, created\n"
            "                      on the first launch and brought back to the lock\n"
            "                      when it changes; the program relaunches inside.\n"
            "                      If a conda / venv environment is active, stops and\n"
            "                      points to --bootstrap=pip|none instead of creating\n"
            "                      a parallel venv.\n"
            "  --bootstrap=force   same, even when another environment is active.\n"
            "  --bootstrap=pip     install the lock in the current Python environment\n"
            "                      (--break-system-packages, then --user, if PEP 668).\n"
            "  --bootstrap=none    install nothing: check that the dependencies of\n"
            "                      requirements.in are there, and explain what is missing.\n"
            "  --installer-deps    install the whole lock (build scripts), then exit.\n"
            "  --help-bootstrap    this help.\n\n"
            "Same as environment variable {variable}=auto|force|pip|none.\n"
            "To remove the venv at any time: {suppression}\n"),
        "mode_valeur_manquante": "--bootstrap requires a value among auto, force, pip and none",
        "mode_valeur_invalide": ("invalid value for --bootstrap: {valeur!r} "
                                 "(expected: auto, force, pip or none)"),
        "titre_none": "Mode --bootstrap=none: auto-install disabled",
        "paquets_absents": "Missing Python packages: {liste}",
        "installer_vous_meme": "Install them yourself, at the exact versions of the lock:",
        "titre_impossible": "ERROR: cannot install the Python dependencies",
        "dernier_message_pip": "Last pip message:\n  {message}",
        "solutions": "Possible solutions:",
        "solution_auto": "1. Let {nom} create its own isolated environment (recommended):",
        "solution_none": ("2. Install the lock into an environment of your own, then "
                          "relaunch with --bootstrap=none:"),
        "titre_env_actif": "Active Python environment detected (conda / venv)",
        "env_actif": "Active environment: {env}",
        "eviter_venv_parallele": "To avoid creating a parallel venv in {dossier}:",
        "ou_desactiver": "(or deactivate the active environment to use the isolated venv)",
        "ou_forcer": "(or --bootstrap=force to create the venv anyway)",
        "installer_ici": "install the dependencies in this environment",
        "deja_la": "dependencies are already there",
        "titre_premier_lancement": "First launch - creating an isolated Python environment",
        "premier_1": "(a few tens of MB once installed). No impact on the system Python.",
        "premier_2": "To remove it: {suppression}",
        "premier_3": "To use a direct install (no venv):",
        "creation_venv": "Creating venv {venv}...",
        "echec_venv": "ERROR creating venv: {erreur}",
        "installer_venv_module": ("Install the Python venv module "
                                  "(on Debian/Ubuntu: sudo apt install python3-venv)."),
        "installation_venv": "Installing dependencies in the venv (3-5 min)...",
        "echec_installation_venv": "ERROR installing the dependencies in the venv:",
        "verifier_connexion": "Check your internet connection, then try:",
        "installe": "✓ Dependencies installed.",
        "relance": "Relaunching in venv...",
        "relance_dans": "Relaunching in venv: {venv}",
        "installation_en_cours": "Installing dependencies: {liste}...",
        "installation_reussie": "✓ Install succeeded ({libelle})",
        "pip_absent": "pip missing, bootstrap via ensurepip...",
        "pip_installe": "pip installed.",
        "echec_pip": "ERROR bootstrap pip: {erreur}",
        "installer_pip": "Install pip manually: https://pip.pypa.io/en/stable/installation/",
        "timeout_pip": "pip install timeout (>1800s, network blocked?)",
        "titre_venv_absent": "ERROR: Python module 'venv' missing",
        "venv_separe": "On Ubuntu/Debian, this module is in a separate package.",
        "installer_une_fois": "Install it with (once):",
        "ou_version": "# or, if you use Python {version} explicitly:",
        "puis_relancer": "Then relaunch the script.",
        "installation_verrou": "Installing the locked dependencies...",
        "echec_verrou": "ERROR: pip could not install the lock:",
        "verrou_installe": "All dependencies installed.",
        "strategie_standard": "standard",
        "strategie_standard_venv": "standard (venv)",
        "strategie_pep668": "--break-system-packages (PEP 668)",
        "strategie_user": "--user (local install)",
    },
    "fr": {
        "aide": (
            "Dépendances au lancement de {nom} depuis ses sources.\n\n"
            "  --bootstrap=auto    (défaut) venv isolé dans {dossier}, créé au premier\n"
            "                      lancement et remis au verrou quand il change ; le\n"
            "                      programme s'y relance. Si un environnement conda ou venv\n"
            "                      est actif, s'arrête et oriente vers --bootstrap=pip|none\n"
            "                      plutôt que de créer un venv parallèle.\n"
            "  --bootstrap=force   idem, même si un autre environnement est actif.\n"
            "  --bootstrap=pip     installe le verrou dans l'environnement Python courant\n"
            "                      (--break-system-packages, puis --user, si PEP 668).\n"
            "  --bootstrap=none    n'installe rien : vérifie que les dépendances de\n"
            "                      requirements.in sont là et explique ce qui manque.\n"
            "  --installer-deps    installe tout le verrou (scripts de build), puis quitte.\n"
            "  --help-bootstrap    cette aide.\n\n"
            "Équivalent : variable d'environnement {variable}=auto|force|pip|none.\n"
            "Pour supprimer le venv à tout moment : {suppression}\n"),
        "mode_valeur_manquante": "--bootstrap demande une valeur parmi auto, force, pip et none",
        "mode_valeur_invalide": ("valeur invalide pour --bootstrap : {valeur!r} "
                                 "(attendu : auto, force, pip ou none)"),
        "titre_none": "Mode --bootstrap=none : installation automatique désactivée",
        "paquets_absents": "Paquets Python absents : {liste}",
        "installer_vous_meme": "Installez-les vous-même, aux versions exactes du verrou :",
        "titre_impossible": "ERREUR : impossible d'installer les dépendances Python",
        "dernier_message_pip": "Dernier message de pip :\n  {message}",
        "solutions": "Solutions possibles :",
        "solution_auto": "1. Laisser {nom} créer son propre environnement isolé (recommandé) :",
        "solution_none": ("2. Installer le verrou dans un environnement à vous, puis relancer "
                          "avec --bootstrap=none :"),
        "titre_env_actif": "Environnement Python actif détecté (conda / venv)",
        "env_actif": "Environnement actif : {env}",
        "eviter_venv_parallele": "Pour éviter de créer un venv parallèle dans {dossier} :",
        "ou_desactiver": "(ou désactivez l'environnement actif pour utiliser le venv isolé)",
        "ou_forcer": "(ou --bootstrap=force pour créer le venv malgré tout)",
        "installer_ici": "installe les dépendances dans cet environnement",
        "deja_la": "si les dépendances y sont déjà",
        "titre_premier_lancement": "Premier lancement : création d'un environnement Python isolé",
        "premier_1": "(quelques dizaines de Mo une fois installé). Sans effet sur le Python système.",
        "premier_2": "Pour le supprimer : {suppression}",
        "premier_3": "Pour une installation directe (sans venv) :",
        "creation_venv": "Création de l'environnement isolé dans {venv}...",
        "echec_venv": "ERREUR à la création du venv : {erreur}",
        "installer_venv_module": ("Installez le module venv de Python "
                                  "(sous Debian/Ubuntu : sudo apt install python3-venv)."),
        "installation_venv": "Installation des dépendances dans le venv (3 à 5 min)...",
        "echec_installation_venv": "ERREUR à l'installation des dépendances dans le venv :",
        "verifier_connexion": "Vérifiez votre connexion, puis essayez :",
        "installe": "✓ Dépendances installées.",
        "relance": "Relance dans le venv...",
        "relance_dans": "Relance dans {venv}...",
        "installation_en_cours": "Installation de : {liste}...",
        "installation_reussie": "✓ Installation réussie ({libelle})",
        "pip_absent": "pip absent, amorçage par ensurepip...",
        "pip_installe": "pip installé.",
        "echec_pip": "ERREUR d'amorçage de pip : {erreur}",
        "installer_pip": "Installez pip à la main : https://pip.pypa.io/en/stable/installation/",
        "timeout_pip": "pip install : délai dépassé (plus de 1800 s, réseau bloqué ?)",
        "titre_venv_absent": "ERREUR : le module Python « venv » est absent",
        "venv_separe": "Sous Ubuntu/Debian, ce module est dans un paquet séparé.",
        "installer_une_fois": "Installez-le (une seule fois) :",
        "ou_version": "# ou, si vous utilisez explicitement Python {version} :",
        "puis_relancer": "Puis relancez le programme.",
        "installation_verrou": "Installation des dépendances verrouillées...",
        "echec_verrou": "ERREUR : pip n'a pas pu installer le verrou :",
        "verrou_installe": "Toutes les dépendances sont installées.",
        "strategie_standard": "standard",
        "strategie_standard_venv": "standard (venv)",
        "strategie_pep668": "--break-system-packages (PEP 668)",
        "strategie_user": "--user (installation locale)",
    },
}


# ---------------------------------------------------------------- lecture du verrou

def nom_normalise(nom: str) -> str:
    """Nom de distribution comparable (PEP 503) : « Pillow » et « pillow »,
    « srtm.py » et « srtm-py » se valent."""
    return re.sub(r"[-_.]+", "-", nom).lower()


def dependances_directes(fichier, *, conditionnelles=False) -> list:
    """Noms des paquets de requirements.in, sans version.

    Un paquet qui porte un marqueur d'environnement (« ; sys_platform ... ») est
    conditionnel : absent à bon droit sur certains systèmes (numba sur les Mac
    Intel, faute de roue). Il n'est rendu que sur demande : le contrôle au
    démarrage ne l'exige donc pas."""
    noms = []
    for ligne in Path(fichier).read_text(encoding="utf-8").splitlines():
        ligne = ligne.split("#", 1)[0].strip()
        if not ligne or ligne.startswith("-"):
            continue
        if ";" in ligne and not conditionnelles:
            continue
        noms.append(re.split(r"[\s<>=!~;\[]", ligne, maxsplit=1)[0])
    return noms


def dependances_absentes(noms, distributions=None) -> list:
    """Ceux de ``noms`` qu'aucune distribution installée ne fournit.

    Lit les métadonnées des paquets installés, sans rien importer : pas de table
    paquet-module à tenir (Pillow s'importe PIL), et aucun module lourd chargé au
    démarrage."""
    if distributions is None:
        import importlib.metadata
        distributions = importlib.metadata.distributions()
    installes = {nom_normalise(d.metadata["Name"] or "") for d in distributions}
    return [nom for nom in noms if nom_normalise(nom) not in installes]


def empreinte_verrou(verrou) -> str:
    return hashlib.sha256(Path(verrou).read_bytes()).hexdigest()


def commande_installation(python, *options, verrou, avec_empreintes=True) -> list:
    """pip install du verrou, empreintes vérifiées (``avec_empreintes=False`` pour
    une liste de paquets non verrouillée)."""
    commande = [str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check"]
    if avec_empreintes:
        commande.append("--require-hashes")
    return commande + ["-r", str(verrou), *options]


# --------------------------------------------------------------- résolution du mode

class Resolution:
    """Résultat pur de l'analyse des options précoces."""

    def __init__(self, mode, argv, aide=False):
        self.mode, self.argv, self.aide = mode, tuple(argv), aide


def resoudre_mode(argv, environnement, variable, texte=None) -> Resolution:
    """Résout le mode et rend une copie nettoyée de ``argv``.

    Ne modifie ni la séquence ni l'environnement reçus. L'aide a priorité sur les
    erreurs. Une valeur invalide est rejetée (ValueError) : le résultat reste
    atomique, et une option suivante ne peut pas être avalée comme valeur de
    ``--bootstrap``."""
    texte = texte or (lambda cle, **valeurs: LIBELLES["en"][cle].format(**valeurs))
    args = list(argv)
    mode = "auto"
    if "--help-bootstrap" in args:
        return Resolution(mode, args, aide=True)

    depuis_env = environnement.get(variable, "").lower().strip()
    if depuis_env in MODES:
        mode = depuis_env

    a_retirer = []
    for index, argument in enumerate(args):
        valeur = None
        if argument.startswith("--bootstrap="):
            valeur = argument.split("=", 1)[1].lower().strip()
            a_retirer.append(index)
        elif argument == "--bootstrap":
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                raise ValueError(texte("mode_valeur_manquante"))
            valeur = args[index + 1].lower().strip()
            a_retirer.extend((index, index + 1))
        if valeur is not None:
            if valeur not in MODES:
                raise ValueError(texte("mode_valeur_invalide", valeur=valeur))
            mode = valeur

    for drapeau, equivalent in ALIAS:
        if drapeau in args:
            mode = equivalent

    for index in sorted(set(a_retirer), reverse=True):
        del args[index]
    for drapeau in [d for d, _ in ALIAS] + ["--help-bootstrap"]:
        while drapeau in args:
            args.remove(drapeau)
    return Resolution(mode, args)


# --------------------------------------------------------------------- le moteur

def _ecrire(texte="") -> None:
    """print() qui ne plante pas sur une console qui ne sait pas afficher les
    cadres (cp1252) : le message passe, au pire en ASCII."""
    try:
        print(texte)
    except UnicodeEncodeError:
        print(str(texte).encode("ascii", "replace").decode("ascii"))


def _boite(titre) -> None:
    _ecrire()
    _ecrire("  ╔" + "═" * 62 + "╗")
    _ecrire("  ║ " + titre.ljust(60) + " ║")
    _ecrire("  ╚" + "═" * 62 + "╝")


class Amorcage:
    """L'amorçage d'une application : ``Amorcage("lidar2map", racine).lancer()``
    tout en haut du point d'entrée, avant l'import de la moindre dépendance.

    nom          nom de l'application ; le venv est ``~/.<nom>/venv``.
    racine       dossier des sources, où se trouvent le verrou et requirements.in.
    variable     variable d'environnement du mode (défaut ``<NOM>_BOOTSTRAP``).
    verrou       fichier verrouillé (défaut requirements.txt) ; ``None`` pour une
                 installation sans verrou, depuis requirements.in (une édition qui
                 ne peut pas utiliser le verrou universel, Python 3.8).
    reutiliser_environnement
                 vrai : en mode ``auto``, si les dépendances sont déjà là et qu'aucun
                 venv de l'application n'existe, on s'en contente (une image Docker,
                 par exemple) ; faux : toujours le venv, pour que le résultat ne
                 dépende pas de ce que la machine a installé par hasard.
    langue       fonction sans argument qui rend ``"fr"`` ou ``"en"`` (défaut en).
    apres_installation
                 fonction appelée une fois les dépendances en place (tous modes sauf
                 ``none``) : lidar2map y rétablit son contexte TLS strict.
    """

    def __init__(self, nom, racine, *, variable=None, verrou="requirements.txt",
                 dependances="requirements.in", reutiliser_environnement=False,
                 langue=None, apres_installation=None):
        self.nom = nom
        self.racine = Path(racine)
        self.variable = variable or re.sub(r"\W", "_", nom).upper() + "_BOOTSTRAP"
        self.fichier_dependances = self.racine / dependances
        self.fichier_verrou = self.racine / verrou if verrou else None
        self.reutiliser_environnement = reutiliser_environnement
        self._langue = langue or (lambda: "en")
        self.apres_installation = apres_installation

    # -- chemins et textes
    @property
    def dossier(self) -> Path:
        return Path.home() / f".{self.nom}"

    @property
    def venv(self) -> Path:
        return self.dossier / "venv"

    @property
    def marque(self) -> str:
        return f"{self.nom}-verrou.sha256"

    def texte(self, cle, **valeurs) -> str:
        libelles = LIBELLES.get(self._langue(), LIBELLES["en"])
        modele = libelles.get(cle) or LIBELLES["en"][cle]
        return modele.format(**valeurs)

    def _suppression(self) -> str:
        if platform.system() == "Windows":
            return f"rmdir /s /q %USERPROFILE%\\.{self.nom}"
        return f"rm -rf ~/.{self.nom}"

    def aide(self) -> str:
        return self.texte("aide", nom=self.nom, variable=self.variable,
                          dossier=f"~/.{self.nom}/venv", suppression=self._suppression())

    # -- verrou
    @property
    def _source(self) -> Path:
        """Ce que pip installe : le verrou, ou à défaut requirements.in."""
        return self.fichier_verrou or self.fichier_dependances

    def empreinte(self) -> str:
        return empreinte_verrou(self._source)

    def commande(self, python, *options) -> list:
        return commande_installation(python, *options, verrou=self._source,
                                     avec_empreintes=self.fichier_verrou is not None)

    def absentes(self) -> list:
        return dependances_absentes(dependances_directes(self.fichier_dependances))

    # -- le mode
    def mode(self) -> str:
        """Résout le mode, nettoie ``sys.argv`` en place, traite l'aide."""
        try:
            resolution = resoudre_mode(sys.argv, os.environ, self.variable, self.texte)
        except ValueError as erreur:
            print(f"ERROR: {erreur}", file=sys.stderr)
            sys.exit(2)
        if resolution.aide:
            _ecrire(self.aide())
            sys.exit(0)
        sys.argv[:] = resolution.argv
        return resolution.mode

    # -- le point d'entrée
    def lancer(self) -> None:
        """Rend la main quand les dépendances sont disponibles dans CE processus,
        se relance dans le venv (sans revenir), ou s'arrête avec un message."""
        mode = self.mode()
        if getattr(sys, "frozen", False):
            # Les dépendances sont dans l'exécutable : pip n'a rien à y faire.
            while OPTION_INSTALLER in sys.argv:
                sys.argv.remove(OPTION_INSTALLER)
            return
        if mode == "none":
            self.verifier()
        elif mode == "pip":
            self.assurer_pip()
        else:
            self.venv_et_relance(force=(mode == "force"))
        if OPTION_INSTALLER in sys.argv:
            sys.argv.remove(OPTION_INSTALLER)
            sys.exit(0 if self.installer_verrou_complet() else 1)
        if mode != "none":
            self.installer_si_besoin()
            if self.apres_installation is not None:
                self.apres_installation()

    # -- mode none
    def verifier(self) -> None:
        manquantes = self.absentes()
        if not manquantes:
            return
        _boite(self.texte("titre_none"))
        _ecrire("  " + self.texte("paquets_absents", liste=", ".join(manquantes)))
        _ecrire()
        _ecrire("  " + self.texte("installer_vous_meme"))
        _ecrire(f"    pip install -r {self._source}")
        _ecrire()
        sys.exit(1)

    # -- mode pip
    def assurer_pip(self) -> None:
        """S'assure que pip est disponible, par ensurepip au besoin."""
        retour = subprocess.run([sys.executable, "-m", "pip", "--version"],
                                capture_output=True)
        if retour.returncode == 0:
            return
        _ecrire("  " + self.texte("pip_absent"))
        try:
            import ensurepip
            ensurepip.bootstrap(upgrade=True)
            _ecrire("  " + self.texte("pip_installe"))
        except Exception as erreur:
            _ecrire("  " + self.texte("echec_pip", erreur=erreur))
            _ecrire("  " + self.texte("installer_pip"))
            sys.exit(1)

    def installer_si_besoin(self) -> None:
        """Installe le verrou dans l'environnement courant s'il manque quelque
        chose. Rapide quand tout est là : lit les métadonnées, rien d'autre.

        Ordre d'essai : standard, ``--break-system-packages`` (PEP 668 : Linux et
        Homebrew récents), ``--user``. Dans un venv, seule la première a un sens.
        S'arrête proprement, avec un message, si toutes échouent."""
        manquantes = self.absentes()
        if not manquantes:
            return
        _ecrire("  " + self.texte("installation_en_cours", liste=", ".join(manquantes)))
        dans_un_venv = (hasattr(sys, "real_prefix")
                        or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix))
        if dans_un_venv:
            strategies = [((), self.texte("strategie_standard_venv"))]
        else:
            strategies = [((), self.texte("strategie_standard")),
                          (("--break-system-packages",), self.texte("strategie_pep668")),
                          (("--user",), self.texte("strategie_user"))]

        derniere = ""
        for options, libelle in strategies:
            try:
                retour = subprocess.run(self.commande(sys.executable, *options),
                                        capture_output=True, text=True, timeout=DELAI_PIP_S)
            except (OSError, subprocess.TimeoutExpired) as erreur:
                derniere = f"{libelle} : {erreur}"
                continue
            if retour.returncode == 0:
                _ecrire("  " + self.texte("installation_reussie", libelle=libelle))
                return
            derniere = "\n  ".join((retour.stderr or retour.stdout or "").strip().split("\n")[-3:])

        _boite(self.texte("titre_impossible"))
        _ecrire("  " + self.texte("paquets_absents", liste=", ".join(manquantes)))
        if derniere:
            _ecrire("  " + self.texte("dernier_message_pip", message=derniere))
        _ecrire()
        _ecrire("  " + self.texte("solutions"))
        _ecrire("    " + self.texte("solution_auto", nom=self.nom))
        _ecrire(f"       python {self.nom}.py --bootstrap=auto")
        _ecrire("    " + self.texte("solution_none"))
        _ecrire(f"       pip install -r {self._source}")
        _ecrire()
        sys.exit(1)

    def installer_verrou_complet(self, *, lancer=None, executable=None, ecrire=_ecrire) -> bool:
        """``--installer-deps`` : le verrou entier dans l'environnement courant
        (celui du venv, après la relance). Rend vrai si pip a réussi. Les scripts de
        build y ajoutent ensuite PyInstaller. Les coutures injectables gardent ce
        chemin testable sans réseau."""
        lancer = lancer or subprocess.run
        executable = executable or sys.executable
        ecrire("  " + self.texte("installation_verrou"))
        retour = lancer(self.commande(executable), capture_output=True, text=True)
        if retour.returncode != 0:
            ecrire("    " + self.texte("echec_verrou"))
            ecrire("    " + (retour.stderr or retour.stdout or "").strip()[-800:])
            return False
        ecrire("  " + self.texte("verrou_installe"))
        return True

    # -- modes auto et force
    def verifier_venv_linux(self) -> None:
        """Sur Debian/Ubuntu, ``python3-venv`` est un paquet système séparé : absent
        d'un Python nu, il fait planter la création du venv sans message clair."""
        if platform.system() != "Linux":
            return
        try:
            import venv  # noqa: F401
            return
        except ImportError:
            pass
        # Couvre le cas où le module est présent mais pas importable d'ici.
        if subprocess.run([sys.executable, "-m", "venv", "--help"],
                          capture_output=True).returncode == 0:
            return
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        _boite(self.texte("titre_venv_absent"))
        _ecrire("  " + self.texte("venv_separe"))
        _ecrire("  " + self.texte("installer_une_fois"))
        _ecrire()
        _ecrire("    sudo apt install python3-venv")
        _ecrire("    " + self.texte("ou_version", version=version))
        _ecrire(f"    sudo apt install python{version}-venv")
        _ecrire()
        _ecrire("  " + self.texte("puis_relancer"))
        sys.exit(1)

    def relancer(self, python_du_venv, est_windows) -> None:
        """Relance le programme avec le Python du venv.

        Unix : ``os.execv`` remplace le processus. Windows : ``execv`` y rend la main
        au shell tout de suite, pendant que le fils tourne (l'invite s'affiche avant
        la sortie) ; on attend donc le fils et on propage son code. Les flux sont
        passés explicitement : sans cela, lancé par une page avec des tubes, le fils
        n'écrit nulle part où la page lise."""
        if est_windows:
            try:
                sys.stdout.flush()
                sys.stderr.flush()
                retour = subprocess.run([str(python_du_venv)] + sys.argv,
                                        stdout=sys.stdout, stderr=sys.stderr, stdin=sys.stdin)
                sys.exit(retour.returncode)
            except KeyboardInterrupt:
                sys.exit(130)
        else:
            os.execv(str(python_du_venv), [str(python_du_venv)] + sys.argv)

    def venv_et_relance(self, force=False) -> None:
        est_windows = platform.system() == "Windows"
        venv = self.venv

        # Déjà dans CE venv : on est la relance, rien d'autre à faire ici.
        try:
            if Path(sys.prefix).resolve() == venv.resolve():
                return
        except OSError:
            pass

        if self.reutiliser_environnement and not force:
            if not self.absentes() and not venv.exists():
                return

        # Un environnement isolé est déjà actif (conda, venv) : en créer un second en
        # silence surprend. On oriente vers les modes adaptés. Détection par les
        # variables standard, déterministe (contrairement à un scan de sys.path).
        actif = os.environ.get("CONDA_PREFIX") or os.environ.get("VIRTUAL_ENV")
        if actif and not force:
            _boite(self.texte("titre_env_actif"))
            _ecrire("  " + self.texte("env_actif", env=actif))
            _ecrire()
            _ecrire("  " + self.texte("eviter_venv_parallele", dossier=f"~/.{self.nom}/"))
            _ecrire(f"    python {self.nom}.py --bootstrap=pip    # "
                    + self.texte("installer_ici"))
            _ecrire(f"    python {self.nom}.py --bootstrap=none   # "
                    + self.texte("deja_la"))
            _ecrire()
            _ecrire("  " + self.texte("ou_desactiver"))
            _ecrire("  " + self.texte("ou_forcer"))
            _ecrire()
            sys.exit(1)

        dossier_bin = venv / ("Scripts" if est_windows else "bin")
        python_du_venv = dossier_bin / ("python.exe" if est_windows else "python")
        marque = venv / self.marque
        empreinte = self.empreinte()

        # Venv installé depuis ce même verrou : on s'y relance, sans pip.
        try:
            a_jour = python_du_venv.exists() and marque.read_text().strip() == empreinte
        except OSError:
            a_jour = False
        if a_jour:
            _ecrire("  " + self.texte("relance_dans", venv=venv))
            self.relancer(python_du_venv, est_windows)
            return  # ne revient pas : execv, ou sys.exit sous Windows

        if not python_du_venv.exists():
            self.verifier_venv_linux()
            _boite(self.texte("titre_premier_lancement"))
            _ecrire("  " + self.texte("premier_1"))
            _ecrire("  " + self.texte("premier_2", suppression=self._suppression()))
            _ecrire("  " + self.texte("premier_3"))
            _ecrire(f"    python {self.nom}.py --bootstrap=pip")
            _ecrire("  " + self.texte("creation_venv", venv=venv))
            try:
                subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
            except (subprocess.CalledProcessError, OSError) as erreur:
                _ecrire("  " + self.texte("echec_venv", erreur=erreur))
                _ecrire("  " + self.texte("installer_venv_module"))
                sys.exit(1)

        # Toutes les dépendances d'un coup, aux versions exactes du verrou, dont pip
        # vérifie les empreintes. Un venv plus ancien est remis au verrou actuel.
        _ecrire("  " + self.texte("installation_venv"))
        commande = self.commande(python_du_venv)
        try:
            retour = subprocess.run(commande, capture_output=True, text=True,
                                    timeout=DELAI_PIP_S)
            erreur = "" if retour.returncode == 0 else (retour.stderr or retour.stdout or "")[-800:]
        except subprocess.TimeoutExpired:
            erreur = self.texte("timeout_pip")
        except OSError as exception:
            erreur = str(exception)
        if erreur:
            _ecrire("  " + self.texte("echec_installation_venv"))
            _ecrire(f"  {erreur.strip()}")
            _ecrire("  " + self.texte("verifier_connexion"))
            _ecrire("    " + subprocess.list2cmdline(commande))
            sys.exit(1)
        marque.write_text(empreinte + "\n")
        _ecrire("  " + self.texte("installe"))
        _ecrire("  " + self.texte("relance"))
        self.relancer(python_du_venv, est_windows)
