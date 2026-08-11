"""Démarrage de la surveillance avec la session, par le mécanisme du système.

Chaque plateforme a le sien, et aucun n'exige de droits d'administrateur tant
qu'on s'en tient à la session courante :

  Windows : un raccourci dans le dossier de démarrage. Le planificateur de
            tâches ferait aussi l'affaire, mais son dossier racine demande une
            élévation, alors que ce raccourci n'en demande jamais.
  macOS   : un agent de lancement, chargé à l'ouverture de session.
  Linux   : un service utilisateur systemd.

Chacun se défait en supprimant un fichier, ce qui est délibéré : un mécanisme
de démarrage qu'on ne sait plus retirer est une nuisance.

L'installation modifie la configuration de votre session : elle n'a lieu que
sur demande explicite, et `--dry-run` montre ce qui serait fait sans le faire.
"""

import argparse
import subprocess
import sys
from pathlib import Path

import runtime


NOM = "blink2video"


def etiquette(quoi: tuple) -> str:
    """Nom de l'entrée, dérivé du verbe : une par verbe automatisé.

    Plusieurs entrées cohabitent, c'est le besoin courant : la boucle qui
    surveille et rapatrie, et l'interface qui reste à disposition. Les nommer
    d'après leur verbe permet d'en retirer une sans toucher aux autres."""
    return f"{NOM}-{(quoi or DEFAUT)[0]}"


# Ce qu'on automatise par défaut : une seule entrée qui lève l'interface puis
# boucle sur le contrôle, le rapatriement et l'assemblage. Rien n'oblige à
# automatiser cela : « autostart on loop watch » n'alerterait que.
DEFAUT = ("loop", "--serve")


def commande(verbe_et_options: tuple = DEFAUT) -> list:
    """Ligne à faire exécuter au démarrage, pour le verbe demandé.

    runtime.self_command sait déjà se relancer correctement selon qu'on tourne
    depuis les sources ou depuis un bundle : c'est exactement ce qu'il faut
    inscrire dans le mécanisme de démarrage.

    Une substitution s'impose toutefois sous Windows quand on tourne depuis les
    sources : python.exe ouvrirait une console noire à chaque ouverture de
    session. pythonw.exe exécute la même chose sans fenêtre, ce que ces
    programmes peuvent se permettre puisqu'ils rendent compte dans watch.log."""
    verbe, *options = verbe_et_options or DEFAUT
    if verbe not in runtime.VERBES:
        raise ValueError(f"verbe inconnu : {verbe}")
    ligne = runtime.self_command(verbe, *options)
    if sys.platform == "win32" and not runtime.frozen():
        sans_fenetre = Path(ligne[0]).with_name("pythonw.exe")
        if sans_fenetre.is_file():
            ligne[0] = str(sans_fenetre)
    return ligne


def appliquer(etat: str, simulation: bool = False, quoi: tuple = DEFAUT) -> int:
    """`on` installe, `off` retire, `status` renseigne."""
    if sys.platform == "win32":
        return _windows(etat, simulation, quoi)
    if sys.platform == "darwin":
        return _macos(etat, simulation, quoi)
    if sys.platform.startswith("linux"):
        return _linux(etat, simulation, quoi)
    print(f"Démarrage automatique non pris en charge sur {sys.platform}.")
    return 1


# ------------------------------------------------------------------- Windows

def _dossier_demarrage() -> Path:
    import ctypes

    tampon = ctypes.create_unicode_buffer(260)
    # CSIDL_STARTUP = 7 : dossier de démarrage de l'utilisateur courant.
    ctypes.windll.shell32.SHGetFolderPathW(None, 7, None, 0, tampon)
    return Path(tampon.value)


def _raccourci(quoi: tuple = ()) -> Path:
    return _dossier_demarrage() / f"{etiquette(quoi)}.lnk"


def _windows(etat: str, simulation: bool, quoi: tuple = DEFAUT) -> int:
    if etat == "status":
        return _lister(sorted(_dossier_demarrage().glob(f"{NOM}*.lnk")),
                       "Raccourcis de démarrage")
    cible = _raccourci(quoi)
    if etat == "off":
        return _retirer(cible, simulation)

    ligne = commande(quoi)
    executable = ligne[0]
    arguments = subprocess.list2cmdline(ligne[1:])
    if simulation:
        print(f"Créerait {cible}")
        print(f"  cible : {executable}")
        print(f"  args  : {arguments}")
        return 0

    # Un raccourci se crée par l'interface COM de l'explorateur, présente sur
    # toute installation de Windows. PowerShell l'expose sans rien installer.
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut({cible});"
        "$s.TargetPath = {executable}; $s.Arguments = {arguments};"
        "$s.WorkingDirectory = {dossier};"
        "$s.Description = 'Surveillance blink2video'; $s.Save()"
    ).format(
        cible=_chaine_ps(str(cible)),
        executable=_chaine_ps(executable),
        arguments=_chaine_ps(arguments),
        dossier=_chaine_ps(str(runtime.app_dir())),
    )
    resultat = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True, errors="replace", check=False,
    )
    if resultat.returncode != 0 or not cible.exists():
        print(f"Échec : {resultat.stderr.strip() or 'raccourci non créé'}")
        return 1
    return _installe(cible, quoi)


def _chaine_ps(valeur: str) -> str:
    """Chaîne littérale PowerShell : seule l'apostrophe se double."""
    return "'" + valeur.replace("'", "''") + "'"


# --------------------------------------------------------------------- macOS

def _macos(etat: str, simulation: bool, quoi: tuple = DEFAUT) -> int:
    dossier = Path.home() / "Library/LaunchAgents"
    if etat == "status":
        return _lister(sorted(dossier.glob(f"com.nico579.{NOM}*.plist")),
                       "Agents de lancement")
    cible = dossier / f"com.nico579.{etiquette(quoi)}.plist"
    if etat == "off":
        if not simulation:
            subprocess.run(["launchctl", "unload", str(cible)], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return _retirer(cible, simulation)

    arguments = "".join(f"    <string>{a}</string>\n" for a in commande())
    contenu = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f'  <key>Label</key><string>com.nico579.{NOM}</string>\n'
        f'  <key>ProgramArguments</key>\n  <array>\n{arguments}  </array>\n'
        f'  <key>WorkingDirectory</key><string>{runtime.app_dir()}</string>\n'
        '  <key>RunAtLoad</key><true/>\n'
        # Relance la surveillance si elle s'interrompt : un chien de garde qui
        # s'arrête en silence ne vaut rien.
        '  <key>KeepAlive</key><true/>\n'
        '</dict></plist>\n'
    )
    if simulation:
        print(f"Écrirait {cible} :\n{contenu}")
        return 0
    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.write_text(contenu, encoding="utf-8")
    subprocess.run(["launchctl", "load", str(cible)], check=False)
    return _installe(cible, quoi)


# --------------------------------------------------------------------- Linux

def _linux(etat: str, simulation: bool, quoi: tuple = DEFAUT) -> int:
    dossier = Path.home() / ".config/systemd/user"
    if etat == "status":
        return _lister(sorted(dossier.glob(f"{NOM}*.service")),
                       "Services utilisateur")
    cible = dossier / f"{etiquette(quoi)}.service"
    if etat == "off":
        if not simulation:
            subprocess.run(["systemctl", "--user", "disable", "--now", etiquette(quoi)],
                           check=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        code = _retirer(cible, simulation)
        if not simulation:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        return code

    contenu = (
        "[Unit]\n"
        "Description=Surveillance blink2video\n\n"
        "[Service]\n"
        f"ExecStart={' '.join(commande())}\n"
        f"WorkingDirectory={runtime.app_dir()}\n"
        "Restart=on-failure\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    if simulation:
        print(f"Écrirait {cible} :\n{contenu}")
        return 0
    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.write_text(contenu, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", etiquette(quoi)],
                   check=False)
    return _installe(cible, quoi)


# ------------------------------------------------------------------- communs

def lue(cible: Path) -> list:
    """Commande réellement inscrite dans le mécanisme installé.

    On la relit plutôt que de la recalculer : ce qui compte pour l'utilisateur
    est ce qui va s'exécuter, pas ce qu'on installerait aujourd'hui."""
    try:
        if cible.suffix == ".lnk":
            import subprocess as sp
            script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
                      + _chaine_ps(str(cible)) + "); "
                      "Write-Output $s.TargetPath; Write-Output $s.Arguments")
            sortie = sp.run(["powershell", "-NoProfile", "-NonInteractive",
                             "-Command", script], stdout=sp.PIPE, text=True,
                            errors="replace", check=False).stdout
            return [l.strip() for l in (sortie or "").splitlines() if l.strip()]
        texte = cible.read_text(encoding="utf-8")
        if cible.suffix == ".service":
            for ligne in texte.splitlines():
                if ligne.startswith("ExecStart="):
                    return ligne.split("=", 1)[1].split()
        if cible.suffix == ".plist":
            import re
            bloc = re.search(r"<array>(.*?)</array>", texte, re.S)
            if bloc:
                return re.findall(r"<string>(.*?)</string>", bloc.group(1))
    except Exception:
        pass
    return []


def _lister(entrees: list, intitule: str) -> int:
    """Toutes les entrées installées, avec ce que chacune lance réellement."""
    if not entrees:
        print(f"{intitule} : aucune")
        return 0
    print(f"{intitule} : {len(entrees)}")
    for cible in entrees:
        print(f"  {cible.name}")
        commande_lue = lue(cible)
        if commande_lue:
            print(f"    lance : {' '.join(commande_lue)}")
    return 0


def _retirer(cible: Path, simulation: bool) -> int:
    if simulation:
        print(f"Supprimerait {cible}")
        return 0
    existait = cible.exists()
    cible.unlink(missing_ok=True)
    print(f"Démarrage automatique {'retiré' if existait else 'déjà absent'} : {cible}")
    return 0


def _installe(cible: Path, quoi: tuple = DEFAUT) -> int:
    print(f"Démarrage automatique installé : {cible}")
    print(f"  commande : {' '.join(commande(quoi))}")
    print("  Il prendra effet à la prochaine ouverture de session.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Démarrage de la surveillance avec la session.",
        epilog="Exemples : blink autostart on | blink autostart status | "
               "blink autostart off --dry-run",
    )
    parser.add_argument("etat", choices=("on", "off", "status"), nargs="?",
                        default="status",
                        help="on installe, off retire, status renseigne (défaut)")
    # nargs="*" plus parse_known_args : les options inconnues d'ici, comme
    # « --port 8899 », rejoignent le verbe, tandis que --dry-run reste compris
    # où qu'il soit placé. REMAINDER avalait --dry-run avec le reste, et une
    # simulation installait pour de bon.
    parser.add_argument("quoi", nargs="*", metavar="VERBE",
                        help="ce qu'il faut lancer à l'ouverture de session, "
                             "avec ses options. Défaut : « watch --loop », qui "
                             "surveille, alerte, rapatrie, assemble et sert "
                             "l'interface")
    parser.add_argument("--dry-run", action="store_true",
                        help="montrer ce qui serait fait sans rien modifier")
    args, restant = parser.parse_known_args()
    quoi = tuple(args.quoi) + tuple(restant)
    try:
        return appliquer(args.etat, args.dry_run, quoi or DEFAUT)
    except ValueError as erreur:
        # Un verbe inconnu mérite un message, pas une trace d'exécution.
        parser.error(str(erreur))


if __name__ == "__main__":
    raise SystemExit(main())
