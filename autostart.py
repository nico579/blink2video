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

import subprocess
import sys
from pathlib import Path

import runtime


NOM = "blink2video"


def commande() -> list:
    """Ligne à faire exécuter au démarrage.

    runtime.self_command sait déjà se relancer correctement selon qu'on tourne
    depuis les sources ou depuis un bundle : c'est exactement ce qu'il faut
    inscrire dans le mécanisme de démarrage."""
    return runtime.self_command("watch", "--loop")


def appliquer(etat: str, simulation: bool = False) -> int:
    """`on` installe, `off` retire, `status` renseigne."""
    if sys.platform == "win32":
        return _windows(etat, simulation)
    if sys.platform == "darwin":
        return _macos(etat, simulation)
    if sys.platform.startswith("linux"):
        return _linux(etat, simulation)
    print(f"Démarrage automatique non pris en charge sur {sys.platform}.")
    return 1


# ------------------------------------------------------------------- Windows

def _raccourci() -> Path:
    import ctypes

    tampon = ctypes.create_unicode_buffer(260)
    # CSIDL_STARTUP = 7 : dossier de démarrage de l'utilisateur courant.
    ctypes.windll.shell32.SHGetFolderPathW(None, 7, None, 0, tampon)
    return Path(tampon.value) / f"{NOM}.lnk"


def _windows(etat: str, simulation: bool) -> int:
    cible = _raccourci()
    if etat == "status":
        return _dire(cible, "Raccourci de démarrage")
    if etat == "off":
        return _retirer(cible, simulation)

    ligne = commande()
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
    return _installe(cible)


def _chaine_ps(valeur: str) -> str:
    """Chaîne littérale PowerShell : seule l'apostrophe se double."""
    return "'" + valeur.replace("'", "''") + "'"


# --------------------------------------------------------------------- macOS

def _macos(etat: str, simulation: bool) -> int:
    cible = Path.home() / "Library/LaunchAgents" / f"com.nico579.{NOM}.plist"
    if etat == "status":
        return _dire(cible, "Agent de lancement")
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
    return _installe(cible)


# --------------------------------------------------------------------- Linux

def _linux(etat: str, simulation: bool) -> int:
    cible = Path.home() / ".config/systemd/user" / f"{NOM}.service"
    if etat == "status":
        return _dire(cible, "Service utilisateur")
    if etat == "off":
        if not simulation:
            subprocess.run(["systemctl", "--user", "disable", "--now", NOM],
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
    subprocess.run(["systemctl", "--user", "enable", "--now", NOM], check=False)
    return _installe(cible)


# ------------------------------------------------------------------- communs

def _dire(cible: Path, intitule: str) -> int:
    print(f"{intitule} : {'présent' if cible.exists() else 'absent'}")
    print(f"  {cible}")
    if cible.exists():
        print(f"  commande : {' '.join(commande())}")
    return 0


def _retirer(cible: Path, simulation: bool) -> int:
    if simulation:
        print(f"Supprimerait {cible}")
        return 0
    existait = cible.exists()
    cible.unlink(missing_ok=True)
    print(f"Démarrage automatique {'retiré' if existait else 'déjà absent'} : {cible}")
    return 0


def _installe(cible: Path) -> int:
    print(f"Démarrage automatique installé : {cible}")
    print(f"  commande : {' '.join(commande())}")
    print("  Il prendra effet à la prochaine ouverture de session.")
    return 0
