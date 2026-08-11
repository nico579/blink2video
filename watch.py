"""Surveille l'installation Blink et prévient par courriel quand elle se dégrade.

Le besoin vient d'un constat : une caméra peut cesser d'enregistrer sans que
rien ne le signale. Le Portail était hors ligne depuis seize jours, découvert
par hasard. Un système de surveillance qui s'arrête en silence est pire qu'une
absence de surveillance, puisqu'on continue de compter dessus.

Ce script compare l'état courant à celui du passage précédent et n'alerte que
sur les dégradations : une caméra qui passe hors ligne, une batterie qui n'est
plus « ok », une détection coupée, un silence anormalement long. Les retours à
la normale sont signalés aussi, mais sans insistance, pour qu'on sache qu'un
incident est clos.

En mode continu (--loop), c'est le verbe qui fait tout : il démarre l'interface
web, puis à chaque tour il contrôle l'état, alerte s'il se dégrade, rapatrie les
nouveaux clips, les assemble, et le signale par une notification cliquable. Trois
interrupteurs permettent de restreindre : --no-serve, --no-download, --no-build.

Sans --loop, il ne fait qu'un contrôle et s'arrête, ce qui convient à un
lancement périodique par un planificateur.
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
import sys
from pathlib import Path

# Avant tout import de dépendance : c'est ici qu'un environnement isolé
# est préparé et le programme relancé dedans si nécessaire.
import runtime

runtime.bootstrap()

from aiohttp import ClientSession

import blink as bk
import merge_daily as md


import runtime

BASE_DIR = runtime.app_dir()
WATCH_STATE = BASE_DIR / ".blink_watch_state.json"

# Au-delà de ce silence, une caméra qui enregistrait est considérée en panne.
# Deux jours plutôt qu'un : un jardin peut rester calme vingt-quatre heures.
SILENCE_DAYS = 2

# Identité applicative empruntée pour émettre les notifications Windows.
POWERSHELL_APP_ID = (r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
                     r"\WindowsPowerShell\v1.0\powershell.exe")



async def read_state(timezone) -> dict:
    """Photographie l'installation : caméras, module, dernier clip connu."""
    async with ClientSession() as session:
        blink = await bk.connect_saved(session)
        if blink is None:
            raise RuntimeError(
                "session Blink absente ou expirée ; relancer « python blink.py login »"
            )
        await blink.refresh(force=True)

        home = blink.homescreen or {}
        raw = {}
        for group in ("cameras", "owls", "doorbells"):
            for item in home.get(group) or []:
                raw[str(item.get("name") or "").strip()] = item

        modules = [
            {"name": str(m.get("name") or "").strip(),
             "online": str(m.get("status") or "") != "offline"}
            for m in (home.get("sync_modules") or [])
        ]

        cameras = {}
        for sync in blink.sync.values():
            for name, camera in sync.cameras.items():
                info = raw.get(name.strip(), {})
                cameras[name.strip()] = {
                    "online": str(info.get("status") or "") != "offline",
                    "armed": bool(camera.motion_enabled
                                  if camera.motion_enabled is not None
                                  else info.get("enabled")),
                    "battery": camera.attributes.get("battery"),
                    "system_armed": bool(sync.arm),
                }

    return {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "modules": modules,
        "cameras": cameras,
        "last_clip": last_clip_per_camera(timezone),
    }


def last_clip_per_camera(timezone) -> dict:
    """Date du dernier clip acquis par caméra, d'après le registre local.

    On lit le registre de téléchargement plutôt que d'interroger Blink : c'est
    gratuit, et c'est bien l'arrivée effective des clips chez soi qui compte."""
    state = md.load_json(BASE_DIR / "Blink_Clips" / md.DOWNLOAD_STATE, {})
    latest: dict = {}
    for entry in (state.get("clips") or {}).values():
        if not isinstance(entry, dict) or entry.get("excluded"):
            continue
        try:
            created = md.parse_created_at(entry["created_at"]).astimezone(timezone)
        except (KeyError, TypeError, ValueError):
            continue
        camera = str(entry.get("camera") or "camera").strip()
        if camera not in latest or created > latest[camera]:
            latest[camera] = created
    return {name: moment.isoformat() for name, moment in latest.items()}


def compare(previous: dict, current: dict, timezone, ignores: set) -> tuple:
    """Établit la liste des dégradations et des retours à la normale.

    Une alerte ne se déclenche que sur un *changement* : sans cela, une caméra
    durablement hors ligne enverrait un courriel à chaque passage et on
    cesserait de les lire, ce qui reviendrait à ne rien surveiller."""
    alerts, recoveries = [], []
    avant = previous.get("cameras") or {}
    # Une caméra explicitement mise en sourdine disparaît de la comparaison :
    # c'est le cas d'un appareil qu'on laisse volontairement hors ligne, ou
    # qu'on a démonté. Elle ne produit ni alerte ni retour à la normale.
    maintenant = {nom: etat for nom, etat in (current.get("cameras") or {}).items()
                  if nom not in ignores}

    for module in current.get("modules") or []:
        etait = next((m for m in previous.get("modules") or []
                      if m["name"] == module["name"]), None)
        if not module["online"] and (etait is None or etait.get("online")):
            alerts.append(f"Module « {module['name']} » hors ligne.")
        elif module["online"] and etait is not None and not etait.get("online"):
            recoveries.append(f"Module « {module['name']} » de nouveau en ligne.")

    for name, etat in sorted(maintenant.items()):
        ancien = avant.get(name) or {}
        if not etat["online"] and (not ancien or ancien.get("online")):
            alerts.append(f"Caméra « {name} » hors ligne.")
        elif etat["online"] and ancien and not ancien.get("online"):
            recoveries.append(f"Caméra « {name} » de nouveau en ligne.")

        if etat["battery"] and etat["battery"] != "ok" and ancien.get("battery") == "ok":
            alerts.append(f"Caméra « {name} » : batterie « {etat['battery']} ».")

        if ancien and ancien.get("armed") and not etat["armed"]:
            alerts.append(f"Caméra « {name} » : détection coupée.")
        elif ancien and not ancien.get("armed") and etat["armed"]:
            recoveries.append(f"Caméra « {name} » : détection réactivée.")

    if maintenant and not any(e["system_armed"] for e in maintenant.values()):
        if not avant or any(e.get("system_armed") for e in avant.values()):
            alerts.append("Système entièrement désarmé.")

    # Silence prolongé : seulement pour une caméra armée et en ligne, sinon on
    # répéterait ce que les alertes précédentes ont déjà dit.
    now = dt.datetime.now(timezone)
    for name, iso in (current.get("last_clip") or {}).items():
        etat = maintenant.get(name) or {}
        if not etat.get("online") or not etat.get("armed"):
            continue
        try:
            jours = (now - dt.datetime.fromisoformat(iso)).days
        except ValueError:
            continue
        deja = 0
        try:
            deja = (dt.datetime.fromisoformat(previous.get("at", now.isoformat()))
                    - dt.datetime.fromisoformat(iso)).days
        except ValueError:
            pass
        if jours >= SILENCE_DAYS > deja:
            alerts.append(f"Caméra « {name} » : aucun clip depuis {jours} jour(s).")

    return alerts, recoveries


def popup(title: str, body: str) -> None:
    """Affiche une boîte de dialogue Windows, sans aucune dépendance.

    C'est la notification qui convient ici : la détection tourne sur la session
    de l'utilisateur, il est donc devant l'écran. Un courriel imposerait un
    serveur SMTP, un mot de passe d'application et une configuration, pour
    joindre quelqu'un qui est déjà là.

    ctypes plutôt qu'une bibliothèque de notifications : MessageBoxW fait
    partie de Windows depuis toujours, rien à installer, rien à maintenir. La
    fenêtre est mise au premier plan, sans quoi elle se perdrait derrière les
    autres et l'alerte passerait inaperçue."""
    if sys.platform != "win32":
        print(f"\n{title}\n{body}")
        return
    import ctypes

    ICONE_AVERTISSEMENT, PREMIER_PLAN, AU_DESSUS = 0x30, 0x10000, 0x40000
    ctypes.windll.user32.MessageBoxW(
        None, body, title, ICONE_AVERTISSEMENT | PREMIER_PLAN | AU_DESSUS
    )


def _applescript(texte: str) -> str:
    """Encode une chaîne pour AppleScript, dont l'échappement n'est pas celui
    d'un shell : seules les guillemets et la barre oblique inverse comptent."""
    return '"' + texte.replace("\\", "\\\\").replace('"', '\\"') + '"'


def toast(titre: str, corps: str, url: str = "") -> None:
    """Notification Windows non bloquante, sans rien installer.

    Distincte du popup à dessein : une coupure est rare et doit être vue, donc
    elle bloque jusqu'à acquittement ; l'arrivée d'un clip est fréquente et
    banale, une fenêtre modale à chaque fois serait insupportable.

    Passe par PowerShell, qui expose l'API de notification de Windows 10. Ça
    évite d'ajouter une dépendance pour une dizaine de lignes, et de réécrire
    à la main un icône de zone de notification en Win32."""
    if sys.platform == "darwin":
        runtime.lancer(
            ["osascript", "-e",
             f"display notification {_applescript(corps)} with title {_applescript(titre)}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=20,
        )
        return
    if sys.platform.startswith("linux"):
        if shutil.which("notify-send"):
            runtime.lancer(["notify-send", titre, corps],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False, timeout=20)
            return
    if sys.platform != "win32":
        print(f"{titre} : {corps}")
        return
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
        + repr(POWERSHELL_APP_ID) +
        ").Show($n)"
    )
    runtime.lancer(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False, timeout=20,
    )


def download_new_clips() -> int:
    """Lance un téléchargement incrémental et renvoie le nombre de clips acquis.

    On relit le compte annoncé par blink.py plutôt que de compter les fichiers :
    c'est lui qui fait autorité, et il distingue déjà le neuf de l'acquis."""
    result = runtime.lancer(
        runtime.self_command("download", "--hub", "Maison"),
        cwd=str(BASE_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"), check=False,
    )
    total = 0
    for ligne in (result.stdout or "").splitlines():
        trouve = re.search(r"Incrémental\s*:\s*(\d+)\s+nouveau", ligne)
        if trouve:
            total += int(trouve.group(1))
    return total


def build_videos() -> bool:
    """Assemble journalières, semaines et mois après l'arrivée de clips.

    Lancé dans la foulée du téléchargement pour que tout soit prêt quand on
    ouvre l'interface : le clip est normalisé, la journée réassemblée. Le
    surcoût est celui d'un seul encodage, les assemblages n'étant que des
    copies de flux."""
    result = runtime.lancer(
        runtime.self_command("merge"),
        cwd=str(BASE_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"), check=False,
    )
    for ligne in (result.stdout or "").splitlines():
        if "échec" in ligne.lower() and "0 échec" not in ligne:
            journal(f"assemblage : {ligne.strip()}")
    return result.returncode == 0


def ensure_server(port: int) -> bool:
    """Démarre l'interface si elle ne tourne pas déjà.

    On teste le port plutôt que de chercher un processus : c'est ce qui
    détermine réellement si la page répond, et ça reste vrai quelle que soit la
    façon dont le serveur a été lancé."""
    import socket

    with socket.socket() as sonde:
        sonde.settimeout(0.5)
        if sonde.connect_ex(("127.0.0.1", port)) == 0:
            return False

    runtime.demarrer(
        runtime.self_command("serve", "--no-browser", "--port", str(port)),
        cwd=str(BASE_DIR), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    journal(f"interface demarree sur le port {port}")
    return True


def journal(ligne: str) -> None:
    """Trace horodatée : lancé par le planificateur, le script n'a pas de console."""
    moment = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with (BASE_DIR / "watch.log").open("a", encoding="utf-8") as fichier:
            fichier.write(f"{moment}  {ligne}\n")
    except OSError:
        pass




def un_tour(args, timezone, a_faire) -> None:
    """Un tour : l'état d'abord, puis les clips, puis l'assemblage.

    L'ordre n'est pas celui de la ligne de commande mais celui des dépendances.
    L'état passe en premier pour qu'une panne soit signalée même si le
    téléchargement échoue ensuite."""
    if "watch" in a_faire:
        _controler(args, timezone)
    if "download" not in a_faire:
        return
    _rapatrier(args, "merge" in a_faire)


def _controler(args, timezone) -> None:
    try:
        current = asyncio.run(read_state(timezone))
    except Exception as error:
        message = f"Impossible d'interroger Blink : {error}"
        journal(message)
        popup("Blink : surveillance en échec", message)
        return

    previous = md.load_json(WATCH_STATE, {})
    ignores = set(previous.get("ignored") or [])
    alerts, recoveries = compare(previous, current, timezone, ignores)
    current["ignored"] = sorted(ignores)
    # Écrire avant de prévenir : la boîte de dialogue attend un clic, et une
    # anomalie non notée serait signalée deux fois au tour suivant.
    if not args.dry_run:
        md.save_json(WATCH_STATE, current)

    moment = dt.datetime.now(timezone).strftime("%d/%m/%Y à %H:%M")
    for ligne in alerts:
        print(f"ALERTE   {ligne}")
    for ligne in recoveries:
        print(f"rétabli  {ligne}")
    if not alerts and not recoveries:
        print(f"Rien à signaler ({moment}).")

    journal("; ".join(alerts + recoveries) or "rien a signaler")
    if alerts and not args.dry_run:
        corps = [f"- {ligne}" for ligne in alerts]
        corps += ["", "Pour ne plus être averti d'une caméra :",
                  '  blink watch --ignore "nom de la caméra"']
        popup(f"Blink : {len(alerts)} anomalie(s)", "\n".join(corps))
    for ligne in recoveries:
        toast("Blink : retour à la normale", ligne)


def _rapatrier(args, assembler: bool) -> None:
    """Rapatrie les nouveaux clips, les assemble, et le signale."""
    try:
        # Le module ne traite qu'une commande à la fois : si l'interface diffuse
        # un direct, on passe notre tour plutôt que d'échouer sur « System is
        # busy ». Le prochain contrôle rattrapera les clips.
        with bk.hub_lock("surveillance"):
            neufs = download_new_clips()
    except bk.BusyError as error:
        journal(f"telechargement reporte : {error}")
        return
    if not neufs:
        return
    journal(f"{neufs} nouveau(x) clip(s)")
    if assembler:
        build_videos()
    pluriel = "s" if neufs > 1 else ""
    toast("Blink",
          f"{neufs} nouveau{'x' if neufs > 1 else ''} clip{pluriel} récupéré{pluriel}"
          f"{'' if assembler else ''}. Cliquez pour ouvrir.",
          url=f"http://127.0.0.1:{args.port}/")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blink watch",
        description="Surveille l'installation Blink et alerte par courriel."
    )
    parser.add_argument("--timezone", default="Europe/Paris")
    runtime.ajouter_boucle(parser)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="afficher les alertes sans envoyer de courriel ni enregistrer l'état",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="déclencher une notification de vérification et s'arrêter",
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="port de l'interface (défaut : 8765)",
    )
    parser.add_argument(
        "--ignore", metavar="CAMERA", nargs="+", default=[],
        help="mettre des caméras en sourdine : plus aucune alerte à leur sujet",
    )
    parser.add_argument(
        "--unignore", metavar="CAMERA", nargs="+", default=[],
        help="lever la sourdine",
    )
    return parser.parse_args()


def main() -> int:
    from zoneinfo import ZoneInfo

    args = parse_args()
    timezone = ZoneInfo(args.timezone)

    # Les sourdines modifient la configuration puis le contrôle se poursuit :
    # une option doit préciser la manière dont la commande travaille, pas la
    # détourner de son objet. C'est le même parti que « merge --exclude », qui
    # écarte un clip puis assemble.
    if args.ignore or args.unignore:
        state = md.load_json(WATCH_STATE, {})
        ignores = set(state.get("ignored") or [])
        ignores |= set(args.ignore)
        ignores -= set(args.unignore)
        state["ignored"] = sorted(ignores)
        md.save_json(WATCH_STATE, state)
        print("Caméras en sourdine :", ", ".join(state["ignored"]) or "aucune")

    if args.test:
        popup("Blink : test d'alerte",
                 "Ceci est un test. La surveillance sait vous joindre.")
        return 0

    # Ce programme contrôle l'état, rien de plus, conformément à son nom. La
    # répétition est une option commune à tous les verbes, pas un verbe.
    return runtime.repeter(
        lambda: un_tour(args, timezone, ("watch",)),
        args.loop, journal,
    )


if __name__ == "__main__":
    raise SystemExit(main())
