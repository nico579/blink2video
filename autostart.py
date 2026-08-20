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


# Ce qu'on automatise par défaut : l'interface, et la boucle complète chaque
# minute. « all » porte lui-même les deux rythmes : le cloud à chaque tour, pour
# un dixième de seconde, et le manifeste USB une fois sur dix, parce qu'il
# réveille le module de synchronisation. Une seule boucle, donc un seul état, un
# seul assemblage et une seule notification. Rien n'oblige à automatiser
# cela : « autostart on watch --loop » n'alerterait que, sans rien rapatrier.
# L'interface n'ouvre pas de navigateur : elle attend qu'on vienne, ou qu'on
# clique sur une notification.
DEFAUT = ("start",)


def commande(verbe_et_options: tuple = DEFAUT) -> list:
    """Ligne à faire exécuter au démarrage, pour le verbe demandé.

    runtime.self_command sait déjà se relancer correctement selon qu'on tourne
    depuis les sources ou depuis un bundle : c'est exactement ce qu'il faut
    inscrire dans le mécanisme de démarrage.

    Une substitution s'impose toutefois sous Windows quand on tourne depuis les
    sources : python.exe ouvrirait une console noire à chaque ouverture de
    session. pythonw.exe exécute la même chose sans fenêtre, ce que ces
    programmes peuvent se permettre puisqu'ils rendent compte dans watch.log."""
    arguments = list(verbe_et_options or DEFAUT)
    if arguments[0] not in runtime.VERBES:
        raise ValueError(f"verbe inconnu : {arguments[0]}")
    # Par le point d'entrée, et non par le programme d'un verbe : lui seul sait
    # lancer plusieurs verbes côte à côte.
    ligne = runtime.commande_composee(arguments)
    if sys.platform == "win32" and not runtime.frozen():
        sans_fenetre = Path(ligne[0]).with_name("pythonw.exe")
        if sans_fenetre.is_file():
            ligne[0] = str(sans_fenetre)
    return ligne


def appliquer_tous(etat: str, simulation: bool, quoi: tuple) -> int:
    """Ordonnance la commande citée, telle quelle.

    « autostart » est un préfixe : il ne fait qu'inscrire au démarrage ce qu'on
    aurait tapé sans lui. Le point d'entrée sachant déjà lancer plusieurs
    verbes, « autostart on serve watch --loop merge --loop 60 » pose une seule
    entrée, qui lancera les trois. L'entrée est nommée d'après le premier
    verbe, ce qui permet d'en tenir plusieurs et d'en retirer une seule."""
    if quoi and "--open-browser" in quoi:
        print("Note : « --open-browser » ouvrira un navigateur à chaque "
              "ouverture de session.")
    if quoi:
        # Vérifie la syntaxe avant d'écrire quoi que ce soit : une entrée de
        # démarrage fautive ne se découvre qu'à l'ouverture de session
        # suivante, quand plus personne ne regarde.
        runtime.decouper_verbes(list(quoi))
    code = appliquer(etat, simulation, quoi or DEFAUT)
    if etat == "status":
        # Ce qui est installé et ce qui tourne sont deux choses : on peut avoir
        # une entrée posée sans instance vivante, ou l'inverse après un
        # lancement à la main.
        instances = runtime.lire_instances()
        if not instances:
            print("En cours  : non")
        for fiche in instances:
            commande = " ".join(" ".join(g) for g in fiche.get("verbes") or [])
            print(f"En cours  : {commande} "
                  f"(PID {fiche['pid']}, depuis {fiche.get('depuis', '?')})")
    return code


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


def est_installe(quoi: tuple = DEFAUT) -> bool:
    """Vrai si le démarrage automatique est actuellement installé.

    `appliquer("status", ...)` imprime et rend un code de sortie qui ne dit
    jamais si c'est actif (0 dans tous les cas, y compris « aucun ») : bon
    pour une CLI, inutilisable tel quel par un appelant programmatique comme
    l'interface web. Même condition que chaque branche `status`, sans rien
    imprimer ni modifier."""
    if sys.platform == "win32":
        return any(_dossier_demarrage().glob(f"{NOM}*.lnk"))
    if sys.platform == "darwin":
        return any((Path.home() / "Library/LaunchAgents").glob(f"com.nico579.{NOM}*.plist"))
    if sys.platform.startswith("linux"):
        return any((Path.home() / ".config/systemd/user").glob(f"{NOM}*.service"))
    return False


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
    # WindowStyle 7 = réduite : depuis un bundle, l'exécutable garde sa console
    # (c'est un outil en ligne de commande), et sans cela elle s'ouvrirait en
    # plein écran à chaque ouverture de session. Réduite, elle reste consultable
    # dans la barre des tâches sans rien recouvrir.
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut({cible});"
        "$s.TargetPath = {executable}; $s.Arguments = {arguments};"
        "$s.WorkingDirectory = {dossier}; $s.WindowStyle = 7;"
        "$s.Description = 'Surveillance blink2video'; $s.Save()"
    ).format(
        cible=_chaine_ps(str(cible)),
        executable=_chaine_ps(executable),
        arguments=_chaine_ps(arguments),
        dossier=_chaine_ps(str(runtime.app_dir())),
    )
    resultat = runtime.lancer(
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
            runtime.lancer(["launchctl", "unload", str(cible)], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return _retirer(cible, simulation)

    arguments = "".join(f"    <string>{a}</string>\n" for a in commande(quoi))
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
    runtime.lancer(["launchctl", "load", str(cible)], check=False)
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
            runtime.lancer(["systemctl", "--user", "disable", "--now", etiquette(quoi)],
                           check=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        code = _retirer(cible, simulation)
        if not simulation:
            runtime.lancer(["systemctl", "--user", "daemon-reload"], check=False)
        return code

    contenu = (
        "[Unit]\n"
        "Description=Surveillance blink2video\n\n"
        "[Service]\n"
        f"ExecStart={' '.join(commande(quoi))}\n"
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
    runtime.lancer(["systemctl", "--user", "daemon-reload"], check=False)
    runtime.lancer(["systemctl", "--user", "enable", "--now", etiquette(quoi)],
                   check=False)
    return _installe(cible, quoi)


# ------------------------------------------------------------------- communs

def lue(cible: Path) -> list:
    """Commande réellement inscrite dans le mécanisme installé.

    On la relit plutôt que de la recalculer : ce qui compte pour l'utilisateur
    est ce qui va s'exécuter, pas ce qu'on installerait aujourd'hui."""
    try:
        if cible.suffix == ".lnk":
            # Un raccourci n'existe que sous Windows, et powershell aussi :
            # la garde est explicite plutôt que déduite de l'extension.
            if sys.platform != "win32":
                return []
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
        prog="blink2video autostart",
        description="Démarrage de la surveillance avec la session.",
        epilog="Exemples : blink2video autostart on | blink2video autostart status | "
               "blink2video autostart off --dry-run",
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
        return appliquer_tous(args.etat, args.dry_run, quoi)
    except ValueError as erreur:
        # Un verbe inconnu mérite un message, pas une trace d'exécution.
        parser.error(str(erreur))


if __name__ == "__main__":
    raise SystemExit(main())
