"""Surveille l'installation Blink et signale les dégradations localement.

Le besoin vient d'un constat : une caméra peut cesser d'enregistrer sans que
rien ne le signale. Le Portail était hors ligne depuis seize jours, découvert
par hasard. Un système de surveillance qui s'arrête en silence est pire qu'une
absence de surveillance, puisqu'on continue de compter dessus.

Ce script compare l'état courant à celui du passage précédent et n'alerte que
sur les dégradations : une caméra qui passe hors ligne, une batterie qui n'est
plus « ok », une détection coupée, un silence anormalement long. Les retours à
la normale sont signalés aussi, mais sans insistance, pour qu'on sache qu'un
incident est clos.

En mode continu (--loop), la boucle commune répète ce contrôle. Le démarrage
de l'interface, le téléchargement et la fusion restent les responsabilités
des autres verbes, réunis par la commande start.

Sans --loop, il ne fait qu'un contrôle et s'arrête, ce qui convient à un
lancement périodique par un planificateur.
"""

import argparse
import asyncio
import datetime as dt
import sys
from collections import Counter

# Avant tout import de dépendance : c'est ici qu'un environnement isolé
# est préparé et le programme relancé dedans si nécessaire.
import runtime

runtime.bootstrap()

import blink_auth
import merge_daily as md


BASE_DIR = runtime.app_dir()
WATCH_STATE = BASE_DIR / ".blink_watch_state.json"

# Au-delà de ce silence, une caméra qui enregistrait est considérée en panne.
# Deux jours plutôt qu'un : un jardin peut rester calme vingt-quatre heures.
SILENCE_DAYS = 2


def _identite_camera(etat: dict):
    """Identité de comparaison, indépendante du nom et de l'ordre de Blink."""
    reseau = str(etat.get("network_id") or "")
    appareil = str(etat.get("device_id") or "")
    return ("device", reseau, appareil) if appareil else (
        "name", reseau, str(etat.get("name") or "").casefold())


def _libelles_cameras(etats: list) -> dict:
    """Ne change les noms visibles que lorsqu'ils sont effectivement ambigus."""
    noms = Counter(etat["name"].casefold() for etat in etats)
    reserves = set(noms)
    cameras = {}
    for etat in sorted(etats, key=lambda e: (e["name"].casefold(), _identite_camera(e))):
        nom = etat["name"]
        if noms[nom.casefold()] > 1:
            suffixe = ", ".join(valeur for valeur in (
                "réseau " + etat["network_id"] if etat.get("network_id") else "",
                "appareil " + etat["device_id"] if etat.get("device_id") else "",
            ) if valeur) or "sans identifiant"
            nom += " [" + suffixe + "]"
            while nom.casefold() in reserves:
                nom += " (2)"
        reserves.add(nom.casefold())
        cameras[nom] = etat
    return cameras


def _cameras_correspondantes(entree: dict, cameras: dict) -> list:
    """Un ancien clip sans identifiants ne désigne jamais deux homonymes."""
    nom = str(entree.get("camera") or "camera").strip().casefold()
    reseau = str(entree.get("network_id") or "")
    appareil = str(entree.get("device_id") or "")
    candidats = []
    for libelle, etat in cameras.items():
        r = str(etat.get("network_id") or "")
        a = str(etat.get("device_id") or "")
        if (reseau and r and reseau != r) or (appareil and a and appareil != a):
            continue
        # Les IDs des deux côtés autorisent un renommage ; sinon le nom
        # reste nécessaire, notamment pour les anciens clips USB sans ID.
        if not (appareil and a and reseau and r):
            if nom != str(etat.get("name") or libelle).strip().casefold():
                continue
        candidats.append(libelle)
    return candidats


def regrouper_entrees_cameras(cameras: dict, entrees: dict) -> dict:
    """Attribue chaque entrée une seule fois, sans arbitrer les ambiguïtés.

    Regroupement local à l'appel : aucun cache ne doit survivre à un changement
    de caméras ou de registre, notamment pour les autorisations de suppression.
    """
    groupes = {nom: {} for nom in cameras}
    for cle, entree in entrees.items():
        if isinstance(entree, dict):
            correspondantes = _cameras_correspondantes(entree, cameras)
            if len(correspondantes) == 1:
                groupes[correspondantes[0]][cle] = entree
    return groupes


def camera_entries(libelle: str, cameras: dict, entrees: dict) -> dict:
    """Entrées attribuables sans ambiguïté à une caméra affichée."""
    return regrouper_entrees_cameras(cameras, entrees).get(libelle, {})


def normaliser_sourdines(ignores, cameras: dict, precedentes=None) -> set:
    """Migre les anciens noms et suit un appareil dont le libellé change."""
    resultat = set()
    for nom in ignores:
        ancien = (precedentes or {}).get(nom) or {}
        if ancien.get("name"):
            correspondants = [cle for cle, etat in cameras.items()
                              if _identite_camera(etat) == _identite_camera(ancien)]
        else:
            correspondants = [cle for cle, etat in cameras.items()
                              if cle == nom or etat.get("name") == nom]
        resultat.update(correspondants or [nom])
    return resultat


def _photos_cameras(blink, home: dict) -> dict:
    bruts = [item for groupe in ("cameras", "owls", "doorbells")
             for item in home.get(groupe) or [] if isinstance(item, dict)]
    objets = []
    for sync in blink.sync.values():
        for nom, camera in sync.cameras.items():
            attrs = camera.attributes or {}
            appareil = (getattr(camera, "device_id", None)
                        or getattr(camera, "camera_id", None)
                        or attrs.get("device_id") or attrs.get("camera_id") or attrs.get("id"))
            objets.append((sync, camera, {
                "name": nom.strip(),
                "network_id": str(getattr(camera, "network_id", None)
                                  or getattr(sync, "network_id", None) or ""),
                "device_id": str(appareil or ""),
            }))
    photos, utilises = [], set()
    for info in bruts:
        meta = {"name": str(info.get("name") or "camera").strip(),
                "network_id": str(info.get("network_id") or info.get("network") or ""),
                "device_id": str(info.get("id") or info.get("device_id")
                                 or info.get("camera_id") or "")}
        candidats = _cameras_correspondantes(
            {**meta, "camera": meta["name"]},
            {str(i): objet[2] for i, objet in enumerate(objets)})
        # Sans ID sur l'objet blinkpy, deux appareils de même nom/réseau
        # restent distincts grâce à homescreen, sans emprunter leurs mesures.
        if len(candidats) == 1:
            index = int(candidats[0])
            sync, camera, objet_meta = objets[index]
            homonymes = [brut for brut in bruts
                         if str(brut.get("name") or "").strip() == meta["name"]
                         and str(brut.get("network_id") or brut.get("network") or "")
                         == meta["network_id"]]
            fiable = bool(objet_meta["device_id"]) or len(homonymes) == 1
            utilises.add(index)
        else:
            sync = next((s for s in blink.sync.values()
                         if str(getattr(s, "network_id", "")) == meta["network_id"]), None)
            camera, fiable = None, False
        enabled = info.get("enabled")
        if enabled is None and fiable:
            enabled = camera.motion_enabled
        photos.append({**meta,
                       "online": str(info.get("status") or "") != "offline",
                       "armed": bool(enabled),
                       "battery": info.get("battery", camera.attributes.get("battery")
                                           if fiable else None),
                       "system_armed": bool(getattr(sync, "arm", False))})
    for index, (sync, camera, meta) in enumerate(objets):
        if index not in utilises:
            photos.append({**meta, "online": True,
                           "armed": bool(camera.motion_enabled),
                           "battery": camera.attributes.get("battery"),
                           "system_armed": bool(sync.arm)})
    return _libelles_cameras(photos)




async def read_state(timezone) -> dict:
    """Photographie l'installation : caméras, module, dernier clip connu."""
    async with blink_auth.session_http_temporaire() as session:
        blink = await blink_auth.connect_saved(session)
        if blink is None:
            raise RuntimeError(
                "session Blink absente ou expirée ; relancer « python blink2video.py login »"
            )
        await blink.refresh(force=True)

        home = blink.homescreen or {}
        modules = [
            {"name": str(m.get("name") or "").strip(),
             "online": str(m.get("status") or "") != "offline"}
            for m in (home.get("sync_modules") or [])
        ]

        cameras = _photos_cameras(blink, home)

    return {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "modules": modules,
        "cameras": cameras,
        "last_clip": last_clip_per_camera(timezone, cameras),
    }


def last_clip_per_camera(timezone, cameras=None) -> dict:
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
        if cameras is not None:
            correspondantes = _cameras_correspondantes(entry, cameras)
            if len(correspondantes) != 1:
                continue
            camera = correspondantes[0]
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


def _etat_camera_precedent(nom: str, etat: dict, avant: dict, cameras: dict) -> dict:
    """Suit l'identité de l'appareil ; le nom seul ne suffit pas aux homonymes."""
    if not etat.get("name"):
        return avant.get(nom) or {}

    ancien = next((e for e in avant.values() if e.get("name")
                   and _identite_camera(e) == _identite_camera(etat)), {})
    # Les anciennes sauvegardes indexées par nom restent utilisables si ce
    # nom est unique dans l'inventaire complet, caméras en sourdine comprises.
    if not ancien and sum(e.get("name") == etat["name"] for e in cameras.values()) == 1:
        candidat = avant.get(etat["name"]) or {}
        if not candidat.get("name"):
            ancien = candidat
    return ancien


def _comparer_camera(nom: str, etat: dict, ancien: dict) -> tuple:
    """Liste les transitions de connexion, batterie et détection, dans cet ordre."""
    alertes, retours = [], []
    # La première observation d'une anomalie compte aussi comme une transition.
    if not etat["online"] and (not ancien or ancien.get("online")):
        alertes.append(_msg("camera_hors_ligne", nom=nom))
    elif etat["online"] and ancien and not ancien.get("online"):
        retours.append(_msg("camera_retour", nom=nom))

    if etat["battery"] and etat["battery"] != "ok" and (
            not ancien or ancien.get("battery") == "ok"):
        alertes.append(_msg("camera_batterie", nom=nom, etat=etat["battery"]))

    if not etat["armed"] and (not ancien or ancien.get("armed")):
        alertes.append(_msg("camera_detection_coupee", nom=nom))
    elif etat["armed"] and ancien and not ancien.get("armed"):
        retours.append(_msg("camera_detection_reactivee", nom=nom))
    return alertes, retours


def _alertes_silence(previous: dict, current: dict, cameras: dict, timezone) -> list:
    """Signale le franchissement du seuil de silence, seulement en ligne et armé."""
    alertes = []
    now = dt.datetime.now(timezone)
    for name, iso in (current.get("last_clip") or {}).items():
        etat = cameras.get(name) or {}
        if not etat.get("online") or not etat.get("armed"):
            continue
        try:
            jours = (now - dt.datetime.fromisoformat(iso)).days
        except ValueError:
            continue
        # Sans date du passage précédent, une caméra déjà silencieuse doit
        # alerter dès sa première observation.
        deja = 0
        previous_at = previous.get("at")
        if previous_at:
            try:
                deja = (dt.datetime.fromisoformat(previous_at)
                        - dt.datetime.fromisoformat(iso)).days
            except ValueError:
                pass
        if jours >= SILENCE_DAYS > deja:
            alertes.append(_msg("camera_silence", nom=name, jours=jours))
    return alertes


def compare(previous: dict, current: dict, timezone, ignores: set) -> tuple:
    """Compare deux observations sans répéter les anomalies déjà signalées.

    Ordre des messages : modules, caméras, système, puis silence prolongé.
    Les caméras en sourdine ne produisent ni alerte ni retour à la normale.
    """
    alerts, recoveries = [], []
    avant = previous.get("cameras") or {}
    cameras = current.get("cameras") or {}
    ignores = normaliser_sourdines(ignores, cameras, avant)
    maintenant = {nom: etat for nom, etat in cameras.items() if nom not in ignores}

    for module in current.get("modules") or []:
        etait = next((m for m in previous.get("modules") or []
                      if m["name"] == module["name"]), None)
        if not module["online"] and (etait is None or etait.get("online")):
            alerts.append(_msg("module_hors_ligne", nom=module["name"]))
        elif module["online"] and etait is not None and not etait.get("online"):
            recoveries.append(_msg("module_retour", nom=module["name"]))

    for name, etat in sorted(maintenant.items()):
        ancien = _etat_camera_precedent(name, etat, avant, cameras)
        alertes_camera, retours_camera = _comparer_camera(name, etat, ancien)
        alerts.extend(alertes_camera)
        recoveries.extend(retours_camera)

    if maintenant and not any(e["system_armed"] for e in maintenant.values()):
        if not avant or any(e.get("system_armed") for e in avant.values()):
            alerts.append(_msg("systeme_desarme"))

    alerts.extend(_alertes_silence(previous, current, maintenant, timezone))
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

    # Même verrou que --ignore/--unignore (main()) : sans lui, un tour de
    # fond qui relit puis réécrit pendant qu'une commande --ignore fait de
    # même perd silencieusement l'une des deux mises à jour (dernier
    # écrivain gagne).
    with runtime.verrou("watch", "controle", stale_after=60, attente=10):
        previous = md.load_json(WATCH_STATE, {})
        ignores = normaliser_sourdines(previous.get("ignored") or [],
                                      current.get("cameras") or {},
                                      previous.get("cameras") or {})
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
        description="Surveille l'installation Blink et signale les anomalies."
    )
    parser.add_argument("--timezone", default="Europe/Paris")
    runtime.ajouter_boucle(parser)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="afficher les alertes sans enregistrer l'état observé",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="déclencher une notification de vérification et s'arrêter",
    )
    parser.add_argument(
        "--port", type=runtime.port_valide, default=8765,
        help="option conservée pour compatibilité ; watch ne démarre plus l'interface",
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
        # Même verrou que _controler() : un tour de fond peut être en train
        # de relire/réécrire WATCH_STATE pendant qu'on modifie la sourdine.
        with runtime.verrou("watch", "sourdine", stale_after=60, attente=10):
            state = md.load_json(WATCH_STATE, {})
            cameras = state.get("cameras") or {}
            ignores = normaliser_sourdines(state.get("ignored") or [], cameras)
            ignores |= normaliser_sourdines(args.ignore, cameras)
            ignores -= normaliser_sourdines(args.unignore, cameras)
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
