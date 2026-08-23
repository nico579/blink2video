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

import blink_auth
import merge_daily as md


import runtime

BASE_DIR = runtime.app_dir()
WATCH_STATE = BASE_DIR / ".blink_watch_state.json"

# Au-delà de ce silence, une caméra qui enregistrait est considérée en panne.
# Deux jours plutôt qu'un : un jardin peut rester calme vingt-quatre heures.
SILENCE_DAYS = 2




async def read_state(timezone) -> dict:
    """Photographie l'installation : caméras, module, dernier clip connu."""
    async with ClientSession() as session:
        blink = await blink_auth.connect_saved(session)
        if blink is None:
            raise RuntimeError(
                "session Blink absente ou expirée ; relancer « python blink2video.py login »"
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
    gratuit, et c'est bien l'arrivée effective des clips chez soi qui compte.

    Un clip écarté (revue de code du 0eab463, bug #11) compte quand même
    pour cette date : « écarté » veut dire que l'utilisateur ne veut pas le
    garder dans les vidéos assemblées, pas que la caméra n'a rien détecté à
    cet instant - l'ignorer ici faisait régresser la dernière activité
    connue vers un clip plus ancien, ou la faisait disparaître entièrement
    si tous les clips récents étaient écartés, au risque d'une fausse
    alerte de silence."""
    state = md.load_json(BASE_DIR / "Blink_Clips" / md.DOWNLOAD_STATE, {})
    latest: dict = {}
    for entry in (state.get("clips") or {}).values():
        if not isinstance(entry, dict):
            continue
        try:
            created = md.parse_created_at(entry["created_at"]).astimezone(timezone)
        except (KeyError, TypeError, ValueError):
            continue
        camera = str(entry.get("camera") or "camera").strip()
        if camera not in latest or created > latest[camera]:
            latest[camera] = created
    return {name: moment.isoformat() for name, moment in latest.items()}


# Suit la langue de la page (runtime.lire_langue(), voir tray.py) : ces
# messages finissent dans une notification ou une boîte de dialogue Windows,
# visibles même la page fermée, donc dans la langue choisie par l'utilisateur,
# pas dans la locale système.
MESSAGES = {
    "fr": {
        "module_hors_ligne": "Module « {nom} » hors ligne.",
        "module_retour": "Module « {nom} » de nouveau en ligne.",
        "camera_hors_ligne": "Caméra « {nom} » hors ligne.",
        "camera_retour": "Caméra « {nom} » de nouveau en ligne.",
        "camera_batterie": "Caméra « {nom} » : batterie « {etat} ».",
        "camera_detection_coupee": "Caméra « {nom} » : détection coupée.",
        "camera_detection_reactivee": "Caméra « {nom} » : détection réactivée.",
        "systeme_desarme": "Système entièrement désarmé.",
        "camera_silence": "Caméra « {nom} » : aucun clip depuis {jours} jour(s).",
        "titre_echec": "Blink : surveillance en échec",
        "titre_anomalies": "Blink : {n} anomalie(s)",
        "titre_retour": "Blink : retour à la normale",
        "hint_sourdine": "Pour ne plus être averti d'une caméra :",
    },
    "en": {
        "module_hors_ligne": 'Module "{nom}" offline.',
        "module_retour": 'Module "{nom}" back online.',
        "camera_hors_ligne": 'Camera "{nom}" offline.',
        "camera_retour": 'Camera "{nom}" back online.',
        "camera_batterie": 'Camera "{nom}": battery "{etat}".',
        "camera_detection_coupee": 'Camera "{nom}": detection disabled.',
        "camera_detection_reactivee": 'Camera "{nom}": detection re-enabled.',
        "systeme_desarme": "System fully disarmed.",
        "camera_silence": 'Camera "{nom}": no clip for {jours} day(s).',
        "titre_echec": "Blink: monitoring failed",
        "titre_anomalies": "Blink: {n} issue(s)",
        "titre_retour": "Blink: back to normal",
        "hint_sourdine": "To stop being notified about a camera:",
    },
}


def _msg(cle: str, **kw) -> str:
    return MESSAGES[runtime.lire_langue()][cle].format(**kw)


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
            alerts.append(_msg("module_hors_ligne", nom=module["name"]))
        elif module["online"] and etait is not None and not etait.get("online"):
            recoveries.append(_msg("module_retour", nom=module["name"]))

    for name, etat in sorted(maintenant.items()):
        ancien = avant.get(name) or {}
        if not etat["online"] and (not ancien or ancien.get("online")):
            alerts.append(_msg("camera_hors_ligne", nom=name))
        elif etat["online"] and ancien and not ancien.get("online"):
            recoveries.append(_msg("camera_retour", nom=name))

        # `not ancien` (premier passage, ou caméra jamais vue avant) compte
        # comme un « ok » implicite ailleurs dans cette fonction (en ligne,
        # armement) ; la batterie ne suivait pas la même règle (revue de
        # code du 0eab463, bug #11) : une caméra déjà faible dès sa première
        # observation ne déclenchait donc jamais rien, jusqu'à ce qu'elle
        # remonte à « ok » puis redescende.
        if etat["battery"] and etat["battery"] != "ok" and (
                not ancien or ancien.get("battery") == "ok"):
            alerts.append(_msg("camera_batterie", nom=name, etat=etat["battery"]))

        if ancien and ancien.get("armed") and not etat["armed"]:
            alerts.append(_msg("camera_detection_coupee", nom=name))
        elif ancien and not ancien.get("armed") and etat["armed"]:
            recoveries.append(_msg("camera_detection_reactivee", nom=name))

    if maintenant and not any(e["system_armed"] for e in maintenant.values()):
        if not avant or any(e.get("system_armed") for e in avant.values()):
            alerts.append(_msg("systeme_desarme"))

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
            alerts.append(_msg("camera_silence", nom=name, jours=jours))

    return alerts, recoveries


toast = runtime.toast


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
        runtime.self_command("serve", "--port", str(port)),
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




def un_tour(args, timezone) -> None:
    """Un contrôle : lire l'état de l'installation, comparer, alerter.

    Rien d'autre. Constater est le travail de ce verbe ; rapatrier et assembler
    sont ceux de « download » et « merge », qui tournent à côté avec leur
    propre cadence."""
    _controler(args, timezone)
    runtime.marquer("watch")

def _controler(args, timezone) -> None:
    try:
        current = asyncio.run(read_state(timezone))
    except Exception as error:
        message = f"Impossible d'interroger Blink : {error}"
        journal(message)
        popup(_msg("titre_echec"), message)
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
        corps += ["", _msg("hint_sourdine"),
                  '  blink2video watch --ignore "nom de la caméra"']
        popup(_msg("titre_anomalies", n=len(alerts)), "\n".join(corps))
    for ligne in recoveries:
        toast(_msg("titre_retour"), ligne)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blink2video watch",
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
        "--port", type=runtime.port_valide, default=8765,
        help="port de l'interface (défaut : 8765)",
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
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # Python 3.8 (build Windows 7, voir build-win7.yml) : pas de zoneinfo en stdlib.
        from backports.zoneinfo import ZoneInfo

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
        lambda: un_tour(args, timezone),
        args.loop, journal,
    )


if __name__ == "__main__":
    raise SystemExit(main())
