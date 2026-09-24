"""Raccourci de bureau : ouvrir ou lancer blink2video en un double-clic.

N'installe rien de permanent, contrairement à autostart.py : un seul fichier
posé sur le Bureau, que l'utilisateur retire lui-même (glisser à la
corbeille) le jour où il n'en veut plus. Pas de pendant « off ».

La commande posée dans le raccourci est simplement « start » : blink_cli
(voir la branche « start » de executer()) se comporte déjà comme « open »
quand une instance écoute déjà sur le port configuré, sans rien relancer.
Le même raccourci sert donc aussi bien à démarrer qu'à rouvrir l'interface
en place, sans composer deux commandes séparées ni ouvrir une seconde
fenêtre de console pour le vérifier - exactement le même verbe, la même
cible, que ce que pose déjà autostart.py pour la session, à ceci près que
« --open-browser » est ajouté ici : absent de la composition standard (elle
n'ouvre jamais de navigateur toute seule, voir DEFAUT dans autostart.py),
il faut l'ajouter explicitement pour ce raccourci-ci, qui n'a de sens que si
l'interface finit par s'afficher."""

import shlex
import subprocess
import sys
from pathlib import Path

import autostart
import runtime

LIBELLES = {
    "fr": {
        "plateforme_non_prise_en_charge":
            "Raccourci de bureau non pris en charge sur {plateforme}.",
        "creerait": "Créerait {cible}",
        "creerait_script": "Créerait {cible} :\n{script}",
        "ecrirait": "Écrirait {cible} :\n{contenu}",
        "cible_label": "  cible : {executable}",
        "args_label": "  args  : {arguments}",
        "echec": "Échec : {detail}",
        "raccourci_non_cree": "raccourci non créé",
        "raccourci_cree": "Raccourci créé : {cible}",
        "description": "Ouvrir blink2video",
    },
    "en": {
        "plateforme_non_prise_en_charge":
            "Desktop shortcut not supported on {plateforme}.",
        "creerait": "Would create {cible}",
        "creerait_script": "Would create {cible}:\n{script}",
        "ecrirait": "Would write {cible}:\n{contenu}",
        "cible_label": "  target: {executable}",
        "args_label": "  args  : {arguments}",
        "echec": "Failed: {detail}",
        "raccourci_non_cree": "shortcut not created",
        "raccourci_cree": "Shortcut created: {cible}",
        "description": "Open blink2video",
    },
}


def _(cle: str, **valeurs) -> str:
    return runtime.traduire(LIBELLES, cle, **valeurs)


def _ligne() -> list:
    # autostart.commande() fait exactement ce qu'il faut : self_command, puis
    # substitution de pythonw.exe à python.exe depuis les sources (seule
    # façon d'obtenir un lancement sans aucune console sous Windows ; un
    # exécutable empaqueté, lui, garde la sienne quoi qu'on fasse - voir la
    # discussion sur console=True dans blink2video.spec).
    return autostart.commande(("start", "--open-browser"))


def _icone() -> Path:
    return runtime.resource_dir() / "assets" / "blink2video.ico"


def creer(simulation: bool = False) -> int:
    """Pose le raccourci sur le Bureau, selon la plateforme."""
    if sys.platform == "win32":
        return _windows(simulation)
    if sys.platform == "darwin":
        return _macos(simulation)
    if sys.platform.startswith("linux"):
        return _linux(simulation)
    print(_("plateforme_non_prise_en_charge", plateforme=sys.platform))
    return 1


# ------------------------------------------------------------------ Windows

def _bureau_windows() -> Path:
    import ctypes
    tampon = ctypes.create_unicode_buffer(1024)
    # CSIDL_DESKTOPDIRECTORY = 0x10 : dossier physique du Bureau de la
    # session courante (distinct de CSIDL_DESKTOP, la racine virtuelle de
    # l'espace de noms du Shell, qui n'est pas un chemin disque).
    ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, tampon)
    return Path(tampon.value)


def _chaine_ps(valeur: str) -> str:
    return "'" + valeur.replace("'", "''") + "'"


def _windows(simulation: bool) -> int:
    cible = _bureau_windows() / "blink2video.lnk"
    ligne = _ligne()
    executable, arguments = ligne[0], subprocess.list2cmdline(ligne[1:])
    if simulation:
        print(_("creerait", cible=cible))
        print(_("cible_label", executable=executable))
        print(_("args_label", arguments=arguments))
        return 0

    # Même mécanisme que autostart.py : l'interface COM de l'explorateur,
    # présente sur toute installation de Windows, exposée sans rien installer
    # depuis PowerShell. WindowStyle 7 = réduite, même choix pour la même
    # raison : un exécutable en mode console (console=True dans le .spec) ne
    # peut pas se lancer sans fenêtre, seulement réduite d'emblée plutôt que
    # de s'ouvrir en plein écran avec tout le journal qui défile - et sans
    # apparaître dans la barre des tâches, comme le fait déjà l'autostart.
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut({cible});"
        "$s.TargetPath = {executable}; $s.Arguments = {arguments};"
        "$s.WorkingDirectory = {dossier}; $s.IconLocation = {icone};"
        "$s.WindowStyle = 7;"
        "$s.Description = {description}; $s.Save()"
    ).format(
        cible=_chaine_ps(str(cible)),
        executable=_chaine_ps(executable),
        arguments=_chaine_ps(arguments),
        dossier=_chaine_ps(str(runtime.app_dir())),
        icone=_chaine_ps(str(_icone())),
        description=_chaine_ps(_("description")),
    )
    resultat = runtime.lancer(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True, errors="replace", check=False,
    )
    if resultat.returncode != 0 or not cible.exists():
        print(_("echec", detail=resultat.stderr.strip() or _("raccourci_non_cree")))
        return 1
    print(_("raccourci_cree", cible=cible))
    return 0


# -------------------------------------------------------------------- macOS

def _macos(simulation: bool) -> int:
    # Un .command ouvrirait toujours Terminal.app, sans moyen de l'éviter :
    # propriété du format, pas un réglage. « do shell script », lui,
    # exécute une commande shell sans jamais montrer de fenêtre - déjà
    # utilisé par runtime._applescript() pour les notifications Windows,
    # même idée ici pour un vrai lancement silencieux. osacompile fabrique
    # un .app à partir de cette unique ligne, sans rien d'autre à embarquer.
    import shutil

    cible = Path.home() / "Desktop" / "blink2video.app"
    commande_shell = "cd {} && {} > /dev/null 2>&1 &".format(
        shlex.quote(str(runtime.app_dir())),
        " ".join(shlex.quote(a) for a in _ligne()))
    script = "do shell script " + runtime._applescript(commande_shell)
    if simulation:
        print(_("creerait_script", cible=cible, script=script))
        return 0
    if cible.exists():
        shutil.rmtree(cible)
    resultat = runtime.lancer(
        ["osacompile", "-o", str(cible), "-e", script],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True, errors="replace", check=False,
    )
    if resultat.returncode != 0 or not cible.exists():
        print(_("echec", detail=resultat.stderr.strip() or _("raccourci_non_cree")))
        return 1
    print(_("raccourci_cree", cible=cible))
    return 0


# -------------------------------------------------------------------- Linux

def _linux(simulation: bool) -> int:
    cible = Path.home() / "Desktop" / "blink2video.desktop"
    exec_ligne = "sh -c {}".format(
        shlex.quote(" ".join(shlex.quote(a) for a in _ligne())))
    contenu = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=blink2video\n"
        f"Exec={exec_ligne}\n"
        f"Path={runtime.app_dir()}\n"
        f"Icon={_icone()}\n"
        "Terminal=true\n"
    )
    if simulation:
        print(_("ecrirait", cible=cible, contenu=contenu))
        return 0
    cible.write_text(contenu, encoding="utf-8")
    cible.chmod(0o755)
    # GNOME/Nautilus refuse de lancer un .desktop du Bureau tant qu'il n'est
    # pas marqué « de confiance » ; les autres environnements (KDE, XFCE) ne
    # connaissent pas cet attribut, d'où l'échec ignoré plutôt que remonté.
    runtime.lancer(["gio", "set", str(cible), "metadata::trusted", "yes"],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(_("raccourci_cree", cible=cible))
    return 0
