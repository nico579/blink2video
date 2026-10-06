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
import os
import subprocess
import sys
from pathlib import Path

from nico579_commons import demarrage
from nico579_commons import relance as relance_commune

import runtime

LIBELLES = {
    "fr": {
        "aide_desc": "Démarrage de la surveillance avec la session.",
        "aide_exemples":
            "Exemples : blink2video autostart on | blink2video autostart status | "
            "blink2video autostart off --dry-run",
        "aide_etat": "on installe, off retire, status renseigne (défaut)",
        "aide_metavar_verbe": "VERBE",
        "aide_quoi":
            "ce qu'il faut lancer à l'ouverture de session, avec ses options. "
            "Défaut : « watch --loop », qui surveille, alerte, rapatrie, "
            "assemble et sert l'interface",
        "aide_dry_run": "montrer ce qui serait fait sans rien modifier",
        "verbe_inconnu": "verbe inconnu : {verbe}",
        "note_open_browser":
            "Note : « --open-browser » ouvrira un navigateur à chaque "
            "ouverture de session.",
        "en_cours_non": "En cours  : non",
        "en_cours_ligne": "En cours  : {commande} (PID {pid}, depuis {depuis})",
        "plateforme_non_prise_en_charge":
            "Démarrage automatique non pris en charge sur {plateforme}.",
        "creerait": "Créerait {cible}",
        "cible_label": "  cible : {executable}",
        "args_label": "  args  : {arguments}",
        "raccourci_non_cree": "raccourci non créé",
        "echec_raccourci": "Échec : {detail}",
        "ecrirait": "Écrirait {cible} :\n{contenu}",
        "label_raccourcis_demarrage": "Raccourcis de démarrage",
        "label_agents_lancement": "Agents de lancement",
        "label_services_utilisateur": "Services utilisateur",
        "intitule_aucune": "{intitule} : aucune",
        "intitule_compte": "{intitule} : {n}",
        "lance_commande": "    lance : {commande}",
        "supprimerait": "Supprimerait {cible}",
        "demarrage_retire": "Démarrage automatique retiré : {cible}",
        "demarrage_deja_absent": "Démarrage automatique déjà absent : {cible}",
        "demarrage_installe": "Démarrage automatique installé : {cible}",
        "installation_refusee":
            "Échec du démarrage automatique : {commande} a répondu {code}. "
            "Fichier laissé en place pour examen : {cible}",
        "plist_invalide":
            "Échec du démarrage automatique : l'agent généré n'est pas un plist "
            "valide ({erreur}). Rien n'a été écrit.",
        "commande_label": "  commande : {commande}",
        "prendra_effet": "  Il prendra effet à la prochaine ouverture de session.",
        "session_systemd_absente":
            "  Aucune session systemd pour {utilisateur} : le service démarrera à "
            "sa prochaine connexion. Pour un démarrage dès l'allumage, sans "
            "connexion : sudo loginctl enable-linger {utilisateur}",
        "agent_migre":
            "Agent de démarrage mis à jour : {cible} (effet à la prochaine "
            "ouverture de session)",
    },
    "en": {
        "aide_desc": "Start monitoring along with the login session.",
        "aide_exemples":
            "Examples: blink2video autostart on | blink2video autostart status | "
            "blink2video autostart off --dry-run",
        "aide_etat": "on installs, off removes, status reports (default)",
        "aide_metavar_verbe": "VERB",
        "aide_quoi":
            "what to run at login, with its options. Default: « watch --loop », "
            "which monitors, alerts, fetches, assembles and serves the interface",
        "aide_dry_run": "show what would be done without changing anything",
        "verbe_inconnu": "unknown verb: {verbe}",
        "note_open_browser":
            "Note: « --open-browser » will open a browser every "
            "time you log in.",
        "en_cours_non": "Running   : no",
        "en_cours_ligne": "Running   : {commande} (PID {pid}, since {depuis})",
        "plateforme_non_prise_en_charge":
            "Autostart not supported on {plateforme}.",
        "creerait": "Would create {cible}",
        "cible_label": "  target: {executable}",
        "args_label": "  args  : {arguments}",
        "raccourci_non_cree": "shortcut not created",
        "echec_raccourci": "Failed: {detail}",
        "ecrirait": "Would write {cible}:\n{contenu}",
        "label_raccourcis_demarrage": "Startup shortcuts",
        "label_agents_lancement": "Launch agents",
        "label_services_utilisateur": "User services",
        "intitule_aucune": "{intitule}: none",
        "intitule_compte": "{intitule}: {n}",
        "lance_commande": "    runs: {commande}",
        "supprimerait": "Would delete {cible}",
        "demarrage_retire": "Autostart removed: {cible}",
        "demarrage_deja_absent": "Autostart already absent: {cible}",
        "demarrage_installe": "Autostart installed: {cible}",
        "installation_refusee":
            "Autostart failed: {commande} returned {code}. "
            "File left in place for inspection: {cible}",
        "plist_invalide":
            "Autostart failed: the generated agent is not a valid plist "
            "({erreur}). Nothing was written.",
        "commande_label": "  command: {commande}",
        "prendra_effet": "  It will take effect at the next login.",
        "session_systemd_absente":
            "  No systemd session for {utilisateur}: the service will start at "
            "their next login. To start at boot without logging in: "
            "sudo loginctl enable-linger {utilisateur}",
        "agent_migre": "Login agent updated: {cible} (takes effect at the next login)",
    },
}


def _(cle: str, **valeurs) -> str:
    return runtime.traduire(LIBELLES, cle, **valeurs)


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
        raise ValueError(_("verbe_inconnu", verbe=arguments[0]))
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
        print(_("note_open_browser"))
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
            print(_("en_cours_non"))
        for fiche in instances:
            commande = " ".join(" ".join(g) for g in fiche.get("verbes") or [])
            print(_("en_cours_ligne", commande=commande, pid=fiche['pid'],
                    depuis=fiche.get('depuis', '?')))
    return code


def appliquer(etat: str, simulation: bool = False, quoi: tuple = DEFAUT) -> int:
    """`on` installe, `off` retire, `status` renseigne."""
    if sys.platform == "win32":
        return _windows(etat, simulation, quoi)
    if sys.platform == "darwin":
        return _macos(etat, simulation, quoi)
    if sys.platform.startswith("linux"):
        return _linux(etat, simulation, quoi)
    print(_("plateforme_non_prise_en_charge", plateforme=sys.platform))
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


def _entree(quoi: tuple = DEFAUT) -> demarrage.Entree:
    """L'entrée de démarrage de ce verbe, pour nico579_commons.demarrage (le
    même code que lidar2map et watch2notif pose les fichiers) : ne reste ici
    que ce qui est propre à blink2video, le nom d'entrée, la commande et le
    dossier de travail."""
    return demarrage.Entree(
        etiquette(quoi), tuple(commande(quoi)), runtime.app_dir(),
        "Surveillance blink2video", label_macos=_label_macos(quoi),
        anciens_labels_macos=(ANCIEN_LABEL_MACOS,))


def _entree_nommee(quoi: tuple = DEFAUT) -> demarrage.Entree:
    """Pour retirer ou lister : le nom suffit, la commande n'est pas évaluée (un
    verbe inconnu ne doit pas empêcher de retirer une entrée)."""
    return demarrage.Entree(etiquette(quoi), (), runtime.app_dir(),
                            label_macos=_label_macos(quoi))


def _message_retrait(cible: Path, existait: bool) -> int:
    print(_("demarrage_retire" if existait else "demarrage_deja_absent", cible=cible))
    return 0


def _windows(etat: str, simulation: bool, quoi: tuple = DEFAUT) -> int:
    if etat == "status":
        return _lister(demarrage.installees(NOM, plateforme="win32"),
                       _("label_raccourcis_demarrage"))
    if etat == "off":
        return _retirer(demarrage.chemin_raccourci(_entree_nommee(quoi)), simulation)

    entree = _entree(quoi)
    cible = demarrage.chemin_raccourci(entree)
    executable = entree.commande[0]
    arguments = subprocess.list2cmdline(entree.commande[1:])
    if simulation:
        print(_("creerait", cible=cible))
        print(_("cible_label", executable=executable))
        print(_("args_label", arguments=arguments))
        return 0

    # Aucune fenêtre ne s'ouvre : l'exécutable est construit sans console
    # (console=False dans blink2video.spec), comme pythonw.exe qui le remplace
    # depuis les sources. WindowStyle 7 (réduite) ne sert plus que de garde.
    try:
        demarrage.activer(entree, plateforme="win32", lancer=runtime.lancer)
    except demarrage.ErreurDemarrage as erreur:
        print(_("echec_raccourci", detail=erreur.valeurs.get("detail") or _("raccourci_non_cree")))
        return 1
    return _installe(cible, quoi)


def _chaine_ps(valeur: str) -> str:
    """Chaîne littérale PowerShell : seule l'apostrophe se double."""
    return "'" + valeur.replace("'", "''") + "'"


# --------------------------------------------------------------------- macOS

# Nom que portaient tous les agents jusqu'à la 0.14 : le même pour chaque
# entrée, alors que launchd identifie un agent par ce nom. Deux entrées ne
# pouvaient donc pas cohabiter, et retirer l'une pouvait décharger l'autre.
# Chaque agent porte désormais le nom de son fichier, selon la convention
# d'Apple, comme ceux de lidar2map et de watch2notif.
ANCIEN_LABEL_MACOS = f"com.nico579.{NOM}"
RELANCE_MACOS = {"SuccessfulExit": False}


def _label_macos(quoi: tuple = DEFAUT) -> str:
    return f"com.nico579.{etiquette(quoi)}"


def _retirer_agent_ancien_nom() -> None:
    """Décharge l'agent encore chargé sous l'ancien nom commun, s'il l'est.

    launchd garde la définition lue à l'ouverture de session. Une fois le
    fichier réécrit (voir migrer_agents_macos), l'agent chargé porte encore
    l'ancien nom : ni « unload » du fichier, ni un nouveau « load », ne le
    retrouveraient, et il relancerait un second superviseur à côté du
    nouveau."""
    runtime.lancer(["launchctl", "remove", ANCIEN_LABEL_MACOS], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def migrer_agents_macos(dossier=None) -> list:
    """Réécrit sur disque les agents posés par une version antérieure.

    Leur nom et leur politique de relance ont changé (issue #31) : sans cette
    réécriture, un agent déjà installé garderait ses défauts jusqu'à ce qu'on
    le réinstalle. launchd ne relit le fichier qu'à l'ouverture de session ;
    la correction prend donc effet à la suivante, sans aucun geste. Même
    principe que la mise à jour de watch2notif. Rend les fichiers réécrits.
    Un fichier illisible reste tel quel : jamais au prix d'un démarrage."""
    import plistlib

    if dossier is None:
        dossier = Path.home() / "Library/LaunchAgents"
    reecrits = []
    for fichier in sorted(Path(dossier).glob(f"com.nico579.{NOM}-*.plist")):
        try:
            agent = plistlib.loads(fichier.read_bytes())
        except Exception:
            continue
        attendu = {"Label": fichier.stem, "KeepAlive": dict(RELANCE_MACOS)}
        if all(agent.get(cle) == valeur for cle, valeur in attendu.items()):
            continue
        agent.update(attendu)
        temporaire = fichier.with_name(fichier.name + ".tmp")
        try:
            temporaire.write_bytes(plistlib.dumps(agent, sort_keys=False))
            os.replace(temporaire, fichier)
        except OSError:
            temporaire.unlink(missing_ok=True)
            continue
        print(_("agent_migre", cible=fichier))
        reecrits.append(fichier)
    return reecrits


def _macos(etat: str, simulation: bool, quoi: tuple = DEFAUT) -> int:
    if etat == "status":
        return _lister(demarrage.installees(NOM, plateforme="darwin",
                                            prefixe_label=f"com.nico579.{NOM}"),
                       _("label_agents_lancement"))
    entree = _entree_nommee(quoi) if etat == "off" else _entree(quoi)
    cible = demarrage.chemin_agent(entree)
    if etat == "off":
        if simulation:
            print(_("supprimerait", cible=cible))
            return 0
        existait = cible.exists()
        # Même sans fichier : un agent chargé sous l'ancien nom commun.
        _retirer_agent_ancien_nom()
        demarrage.desactiver(entree, plateforme="darwin", lancer=runtime.lancer)
        return _message_retrait(cible, existait)

    # Valeurs échappées par plistlib : un dossier comme « Blink & Videos »
    # écrivait un & brut, et launchd refusait le plist (audit du 26/09/2026,
    # B08). Relu avant d'être écrit : jamais d'agent annoncé installé que
    # launchd refuserait à la prochaine ouverture de session.
    import plistlib
    try:
        contenu = demarrage.contenu_agent(entree)
        plistlib.loads(contenu.encode("utf-8"))
    except Exception as erreur:
        print(_("plist_invalide", erreur=erreur))
        return 1
    if simulation:
        print(_("ecrirait", cible=cible, contenu=contenu))
        return 0
    demarrage.activer(entree, plateforme="darwin", lancer=runtime.lancer)
    return _installe(cible, quoi)


# --------------------------------------------------------------------- Linux

# Ces deux-là sont celles du commun (nico579_commons.demarrage), nées ici :
# l'environnement de « systemctl --user » (issue #23) et un argument d'ExecStart=
# entre guillemets, % et $ doublés (audit du 26/09/2026, B07).
env_systemctl = demarrage.env_systemctl
argument_systemd = demarrage.argument_systemd


# Sous l'unité posée par « autostart on », tout processus lancé par blink2video
# reste dans le cgroup de l'unité, quel que soit son parent : start_new_session
# change de session, pas de cgroup. Quand « stop » fait sortir le processus
# principal, systemd clôt l'unité et tue ce qui reste dans ce cgroup, y compris
# ce qui devait remplacer les fichiers d'une mise à jour ou relancer
# blink2video après Appliquer ; et comme la sortie se fait avec le code 0,
# Restart=on-failure ne relance rien (issue #35, pendant Linux de l'issue #31).
UNITE_ENV = "BLINK_UNITE_SYSTEMD"


def unite_systemd(cgroup: Path = Path("/proc/self/cgroup")) -> str:
    """Unité blink2video (« blink2video-start.service ») dont ce processus
    fait partie, ou "" : hors Linux, hors d'une telle unité, ou cgroup
    illisible. La lecture du cgroup est celle de nico579-commons, d'où cette
    fonction est née (issue #35) ; ne reste ici que le nom de l'application."""
    return relance_commune.unite_systemd(NOM, cgroup)


def sortir_du_service(commande: list, environ=None) -> bool:
    """Relance ``commande`` hors de l'unité blink2video dont ce processus
    fait partie, et rend True : l'appelant s'arrête alors là, la suite
    s'exécute dans le processus relancé. Rend False hors d'une telle unité,
    une fois déjà sorti, ou si systemd-run ne répond pas : l'appelant
    continue alors comme avant.

    systemd-run --user --scope place la commande dans une unité « scope »
    transitoire, son propre cgroup, que la fin du service n'atteint pas. Un
    premier essai à vide vérifie que la sortie fonctionne vraiment : sans
    lui, un systemd-run qui échouerait après notre départ ne laisserait plus
    personne pour finir le travail. Les deux gestes, l'essai et le préfixe,
    sont ceux de nico579_commons.relance.hors_du_service ; ne reste ici que
    ce qui est propre à blink2video : le nom de l'unité quittée voyage dans
    BLINK_UNITE_SYSTEMD, pour relancer_service()."""
    env = dict(os.environ if environ is None else environ)
    unite = unite_systemd()
    if not unite or env.get(UNITE_ENV):
        return False
    env = env_systemctl(env)
    a_lancer = relance_commune.hors_du_service(
        commande, nom=NOM, unite=unite,
        lancer=lambda cmd, **options: runtime.lancer(cmd, env=env, **options))
    if a_lancer == list(commande):
        return False
    env[UNITE_ENV] = unite
    runtime.demarrer(a_lancer, env=env, stdin=subprocess.DEVNULL)
    return True


def relancer_service(environ=None) -> str:
    """Relance par systemd l'unité quittée par sortir_du_service() : start
    reprend ainsi sous la garde de son service, plutôt que détaché dans le
    scope transitoire, et la prochaine ouverture de session trouve l'unité
    dans l'état attendu. Rend le nom de l'unité si systemctl l'a acceptée,
    "" sinon : l'appelant relance alors comme avant."""
    env = dict(os.environ if environ is None else environ)
    unite = env.pop(UNITE_ENV, "")
    if not unite:
        return ""
    try:
        resultat = runtime.lancer(["systemctl", "--user", "start", unite],
                                  check=False, env=env_systemctl(env),
                                  stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return unite if resultat.returncode == 0 else ""


def _linux(etat: str, simulation: bool, quoi: tuple = DEFAUT) -> int:
    if etat == "status":
        return _lister(demarrage.installees(NOM, plateforme="linux"),
                       _("label_services_utilisateur"))
    entree = _entree_nommee(quoi) if etat == "off" else _entree(quoi)
    cible = demarrage.chemin_unite(entree)
    env = env_systemctl() if not simulation else None
    if etat == "off":
        if simulation:
            print(_("supprimerait", cible=cible))
            return 0
        existait = cible.exists()
        demarrage.desactiver(entree, plateforme="linux", lancer=runtime.lancer, env=env)
        return _message_retrait(cible, existait)

    if simulation:
        print(_("ecrirait", cible=cible, contenu=demarrage.contenu_unite(entree)))
        return 0
    try:
        notes = demarrage.activer(entree, plateforme="linux", lancer=runtime.lancer, env=env)
    except demarrage.ErreurDemarrage as erreur:
        # Avec une session systemd, un refus est un vrai échec : ne plus
        # l'annoncer comme une installation réussie (audit du 26/09/2026, B07).
        v = erreur.valeurs
        print(_("installation_refusee", commande=v["commande"], code=v["retour"],
                cible=v["cible"]))
        return 1
    code = _installe(cible, quoi)
    for cle, valeurs in notes:
        if cle == "session_systemd_absente":
            print(_("session_systemd_absente", utilisateur=valeurs["utilisateur"]))
    return code


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
        print(_("intitule_aucune", intitule=intitule))
        return 0
    print(_("intitule_compte", intitule=intitule, n=len(entrees)))
    for cible in entrees:
        print(f"  {cible.name}")
        commande_lue = lue(cible)
        if commande_lue:
            print(_("lance_commande", commande=" ".join(commande_lue)))
    return 0


def _retirer(cible: Path, simulation: bool) -> int:
    if simulation:
        print(_("supprimerait", cible=cible))
        return 0
    existait = cible.exists()
    cible.unlink(missing_ok=True)
    print(_("demarrage_retire" if existait else "demarrage_deja_absent", cible=cible))
    return 0


def _installe(cible: Path, quoi: tuple = DEFAUT) -> int:
    print(_("demarrage_installe", cible=cible))
    print(_("commande_label", commande=" ".join(commande(quoi))))
    print(_("prendra_effet"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="blink2video autostart",
        description=_("aide_desc"),
        epilog=_("aide_exemples"),
    )
    parser.add_argument("etat", choices=("on", "off", "status"), nargs="?",
                        default="status", help=_("aide_etat"))
    # nargs="*" plus parse_known_args : les options inconnues d'ici, comme
    # « --port 8899 », rejoignent le verbe, tandis que --dry-run reste compris
    # où qu'il soit placé. REMAINDER avalait --dry-run avec le reste, et une
    # simulation installait pour de bon.
    parser.add_argument("quoi", nargs="*", metavar=_("aide_metavar_verbe"),
                        help=_("aide_quoi"))
    parser.add_argument("--dry-run", action="store_true", help=_("aide_dry_run"))
    args, restant = parser.parse_known_args()
    quoi = tuple(args.quoi) + tuple(restant)
    try:
        return appliquer_tous(args.etat, args.dry_run, quoi)
    except ValueError as erreur:
        # Un verbe inconnu mérite un message, pas une trace d'exécution.
        parser.error(str(erreur))


if __name__ == "__main__":
    raise SystemExit(main())
