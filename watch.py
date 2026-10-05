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
# Le registre des téléchargements vit avec les clips, dans les sorties.
CLIPS = md.DEFAULT_INPUT

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
             "id": str(m.get("id") or ""),
             "network_id": str(m.get("network_id") or ""),
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
    state = md.load_json(CLIPS / md.DOWNLOAD_STATE, {})
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
        "aide_desc": "Surveille l'installation Blink et signale les anomalies.",
        "aide_dry_run": "afficher les alertes sans enregistrer l'état observé",
        "aide_test": "déclencher une notification de vérification et s'arrêter",
        "aide_port":
            "option conservée pour compatibilité ; watch ne démarre plus l'interface",
        "aide_ignore": "mettre des caméras en sourdine : plus aucune alerte à leur sujet",
        "aide_unignore": "lever la sourdine",
        "aide_ignore_module":
            "mettre des Sync Modules en sourdine (identifiant, réseau ou nom) : "
            "plus aucune alerte à leur sujet",
        "aide_unignore_module": "lever la sourdine d'un Sync Module",
        "module_hors_ligne": "Module « {nom} » hors ligne.",
        "module_retour": "Module « {nom} » de nouveau en ligne.",
        "camera_hors_ligne": "Caméra « {nom} » hors ligne.",
        "camera_retour": "Caméra « {nom} » de nouveau en ligne.",
        "camera_batterie": "Caméra « {nom} » : batterie « {etat} ».",
        "camera_detection_coupee": "Caméra « {nom} » : détection coupée.",
        "camera_detection_reactivee": "Caméra « {nom} » : détection réactivée.",
        "systeme_desarme": "Système entièrement désarmé.",
        "camera_silence": "Caméra « {nom} » : aucun clip depuis {jours} jour(s).",
        "camera_jamais_enregistree":
            "Caméra « {nom} » : aucun clip enregistré depuis {jours} jour(s) de surveillance.",
        "titre_echec": "Blink : surveillance en échec",
        "titre_anomalies": "Blink : {n} anomalie(s)",
        "titre_retour": "Blink : retour à la normale",
        "hint_sourdine": "Pour ne plus être averti d'une caméra :",
        "hint_sourdine_module": "Pour ne plus être averti d'un module :",
        "format_moment": "%d/%m/%Y à %H:%M",
        "alerte_ligne": "ALERTE   {ligne}",
        "retabli_ligne": "rétabli  {ligne}",
        "rien_a_signaler": "Rien à signaler ({moment}).",
        "cameras_en_sourdine": "Caméras en sourdine :",
        "modules_en_sourdine": "Modules en sourdine :",
        "module_inconnu":
            "Module inconnu : « {ref} ». Modules connus : {connus}.",
        "module_ambigu":
            "« {ref} » désigne plusieurs modules ({candidats}) : utilisez l'identifiant.",
        "aucune": "aucune",
        "titre_test_alerte": "Blink : test d'alerte",
        "corps_test_alerte": "Ceci est un test. La surveillance sait vous joindre.",
    },
    "en": {
        "aide_desc": "Monitors the Blink installation and reports anomalies.",
        "aide_dry_run": "show alerts without saving the observed state",
        "aide_test": "send a test notification and exit",
        "aide_port": "kept for compatibility; watch no longer starts the interface",
        "aide_ignore": "mute cameras: no more alerts about them",
        "aide_unignore": "unmute",
        "aide_ignore_module":
            "mute Sync Modules (id, network or name): no more alerts about them",
        "aide_unignore_module": "unmute a Sync Module",
        "module_hors_ligne": 'Module "{nom}" offline.',
        "module_retour": 'Module "{nom}" back online.',
        "camera_hors_ligne": 'Camera "{nom}" offline.',
        "camera_retour": 'Camera "{nom}" back online.',
        "camera_batterie": 'Camera "{nom}": battery "{etat}".',
        "camera_detection_coupee": 'Camera "{nom}": detection disabled.',
        "camera_detection_reactivee": 'Camera "{nom}": detection re-enabled.',
        "systeme_desarme": "System fully disarmed.",
        "camera_silence": 'Camera "{nom}": no clip for {jours} day(s).',
        "camera_jamais_enregistree":
            'Camera "{nom}": no clip recorded in {jours} day(s) of monitoring.',
        "titre_echec": "Blink: monitoring failed",
        "titre_anomalies": "Blink: {n} issue(s)",
        "titre_retour": "Blink: back to normal",
        "hint_sourdine": "To stop being notified about a camera:",
        "hint_sourdine_module": "To stop being notified about a module:",
        "format_moment": "%d/%m/%Y at %H:%M",
        "alerte_ligne": "ALERT    {ligne}",
        "retabli_ligne": "fixed    {ligne}",
        "rien_a_signaler": "Nothing to report ({moment}).",
        "cameras_en_sourdine": "Muted cameras:",
        "modules_en_sourdine": "Muted modules:",
        "module_inconnu": 'Unknown module: "{ref}". Known modules: {connus}.',
        "module_ambigu":
            '"{ref}" matches several modules ({candidats}): use the id.',
        "aucune": "none",
        "titre_test_alerte": "Blink: alert test",
        "corps_test_alerte": "This is a test. Monitoring can reach you.",
    },
}


def _msg(cle: str, **kw) -> str:
    return runtime.traduire(MESSAGES, cle, **kw)


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


def suivre_premiers_releves(previous: dict, current: dict) -> dict:
    """Date du premier relevé de chaque caméra qui n'a encore aucun clip.

    `last_clip` ne contient que les caméras ayant déjà enregistré : une caméra
    qui n'a jamais rien enregistré n'y entrait jamais, et le contrôle de
    silence ne la voyait pas, quelle que soit la durée. Ce point d'ancrage
    (depuis quand l'observe-t-on sans clip ?) manquait. Une caméra qui obtient
    un clip, ou disparaît de l'installation, sort du suivi d'elle-même."""
    connues = previous.get("first_seen") or {}
    avec_clip = current.get("last_clip") or {}
    maintenant = current.get("at") or dt.datetime.now(dt.timezone.utc).isoformat()
    return {nom: connues.get(nom) or maintenant
            for nom in (current.get("cameras") or {}) if nom not in avec_clip}


def _alertes_jamais_enregistre(previous: dict, current: dict, cameras: dict,
                               timezone) -> list:
    """Même seuil et mêmes conditions que _alertes_silence : en ligne, armée,
    et une seule alerte au franchissement."""
    alertes = []
    now = dt.datetime.now(timezone)
    for nom, iso in sorted((current.get("first_seen") or {}).items()):
        etat = cameras.get(nom) or {}
        if not etat.get("online") or not etat.get("armed"):
            continue
        try:
            depuis = dt.datetime.fromisoformat(iso)
            jours = (now - depuis).days
            deja = 0
            if previous.get("at"):
                deja = (dt.datetime.fromisoformat(previous["at"]) - depuis).days
        except (ValueError, TypeError):
            continue
        if jours >= SILENCE_DAYS > deja:
            alertes.append(_msg("camera_jamais_enregistree", nom=nom, jours=jours))
    return alertes


def cle_module(module: dict) -> str:
    """Ce qui identifie un module dans la liste des sourdines : son id Blink,
    sinon son nom (module sans id, état d'avant)."""
    return module.get("id") or module["name"]


def resoudre_modules(references, modules: list):
    """(clés, inconnus, ambigus) : désigne des modules par identifiant, réseau,
    nom ou libellé « nom (réseau) ». Un nom partagé par deux modules est
    ambigu : il couperait les deux (les noms par défaut sont identiques)."""
    cles, inconnus, ambigus = set(), [], []
    for ref in references:
        trouves = [m for m in modules if ref in (
            m.get("id"), m.get("network_id"), m["name"], _libelle_module(m, modules))
            and ref]
        if not trouves:
            inconnus.append(ref)
        elif len({cle_module(m) for m in trouves}) > 1:
            ambigus.append((ref, trouves))
        else:
            cles.add(cle_module(trouves[0]))
    return cles, inconnus, ambigus


def _module_precedent(module: dict, anciens: list):
    """Entrée précédente du même module, ou None si on ne peut pas l'affirmer.

    Par identifiant Blink quand les deux côtés en ont un : deux Sync Modules
    peuvent porter le même nom (« My Blink Sync Module » par défaut). Retrouvés
    par le nom, le module hors ligne était comparé au module en ligne qui porte
    le même nom, et sa chute était signalée à chaque tour (fenêtre toutes les
    dix minutes). Sans identifiant (état d'avant, ou module sans id), le nom ne
    sert que s'il désigne un seul module."""
    cle = module.get("id")
    if cle:
        for ancien in anciens:
            if ancien.get("id") == cle:
                return ancien
    memes = [a for a in anciens
             if a["name"] == module["name"] and not (cle and a.get("id"))]
    return memes[0] if len(memes) == 1 else None


def _libelle_module(module: dict, modules: list) -> str:
    """Le nom, suivi du réseau quand deux modules portent le même nom."""
    homonymes = [m for m in modules if m["name"] == module["name"]]
    if len(homonymes) > 1 and module.get("network_id"):
        return f"{module['name']} ({module['network_id']})"
    return module["name"]


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

    modules = current.get("modules") or []
    muets = set(previous.get("ignored_modules") or [])
    for module in modules:
        if cle_module(module) in muets or module["name"] in muets:
            # Comme une caméra en sourdine : ni alerte ni retour à la normale.
            continue
        etait = _module_precedent(module, previous.get("modules") or [])
        nom = _libelle_module(module, modules)
        if not module["online"] and (etait is None or etait.get("online")):
            alerts.append(_msg("module_hors_ligne", nom=nom))
        elif module["online"] and etait is not None and not etait.get("online"):
            recoveries.append(_msg("module_retour", nom=nom))

    for name, etat in sorted(maintenant.items()):
        ancien = _etat_camera_precedent(name, etat, avant, cameras)
        alertes_camera, retours_camera = _comparer_camera(name, etat, ancien)
        alerts.extend(alertes_camera)
        recoveries.extend(retours_camera)

    if maintenant and not any(e["system_armed"] for e in maintenant.values()):
        if not avant or any(e.get("system_armed") for e in avant.values()):
            alerts.append(_msg("systeme_desarme"))

    alerts.extend(_alertes_silence(previous, current, maintenant, timezone))
    alerts.extend(_alertes_jamais_enregistre(previous, current, maintenant, timezone))
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
        current["first_seen"] = suivre_premiers_releves(previous, current)
        alerts, recoveries = compare(previous, current, timezone, ignores)
        current["ignored"] = sorted(ignores)
        current["ignored_modules"] = sorted(previous.get("ignored_modules") or [])
        # Écrire avant de prévenir : la boîte de dialogue attend un clic, et une
        # anomalie non notée serait signalée deux fois au tour suivant.
        if not args.dry_run:
            md.save_json(WATCH_STATE, current)

    moment = dt.datetime.now(timezone).strftime(_msg("format_moment"))
    for ligne in alerts:
        print(_msg("alerte_ligne", ligne=ligne))
    for ligne in recoveries:
        print(_msg("retabli_ligne", ligne=ligne))
    if not alerts and not recoveries:
        print(_msg("rien_a_signaler", moment=moment))

    journal("; ".join(alerts + recoveries) or "rien a signaler")
    if alerts and not args.dry_run:
        corps = [f"- {ligne}" for ligne in alerts]
        modules = current.get("modules") or []
        a_signaler = [
            m for m in modules if not m["online"]
            and _msg("module_hors_ligne", nom=_libelle_module(m, modules)) in alerts]
        if len(a_signaler) < len(alerts):
            corps += ["", _msg("hint_sourdine"),
                      '  blink2video watch --ignore "nom de la caméra"']
        if a_signaler:
            corps += ["", _msg("hint_sourdine_module")]
            corps += [f'  blink2video watch --ignore-module "{cle_module(m)}"'
                      for m in a_signaler]
        popup(_msg("titre_anomalies", n=len(alerts)), "\n".join(corps))
    for ligne in recoveries:
        toast(_msg("titre_retour"), ligne)


def _sourdine_modules(args) -> int:
    """--ignore-module / --unignore-module : 0 si l'état a été mis à jour, 2 si
    un module n'existe pas ou est ambigu (rien n'est alors écrit)."""
    with runtime.verrou("watch", "sourdine", stale_after=60, attente=10):
        state = md.load_json(WATCH_STATE, {})
        modules = state.get("modules") or []
        muets = set(state.get("ignored_modules") or [])
        a_ajouter, inconnus, ambigus = resoudre_modules(args.ignore_module, modules)
        a_retirer, inconnus_retrait, ambigus_retrait = resoudre_modules(
            args.unignore_module, modules)
        # Lever la sourdine d'une clé encore enregistrée reste possible même si
        # le module a disparu de l'installation.
        for ref in list(inconnus_retrait):
            if ref in muets:
                a_retirer.add(ref)
                inconnus_retrait.remove(ref)
        inconnus += inconnus_retrait
        ambigus += ambigus_retrait
        if inconnus or ambigus:
            connus = ", ".join(
                f"{_libelle_module(m, modules)} [{cle_module(m)}]" for m in modules) \
                or _msg("aucune")
            for ref in inconnus:
                print(_msg("module_inconnu", ref=ref, connus=connus))
            for ref, trouves in ambigus:
                candidats = ", ".join(cle_module(m) for m in trouves)
                print(_msg("module_ambigu", ref=ref, candidats=candidats))
            return 2
        muets = (muets | a_ajouter) - a_retirer
        state["ignored_modules"] = sorted(muets)
        md.save_json(WATCH_STATE, state)
    print(_msg("modules_en_sourdine"), ", ".join(state["ignored_modules"]) or _msg("aucune"))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blink2video watch",
        description=_msg("aide_desc"),
    )
    parser.add_argument("--timezone", default="Europe/Paris")
    runtime.ajouter_boucle(parser)
    parser.add_argument("--dry-run", action="store_true", help=_msg("aide_dry_run"))
    parser.add_argument("--test", action="store_true", help=_msg("aide_test"))
    parser.add_argument(
        "--port", type=runtime.port_valide, default=8765, help=_msg("aide_port"),
    )
    parser.add_argument(
        "--ignore", metavar="CAMERA", nargs="+", default=[], help=_msg("aide_ignore"),
    )
    parser.add_argument(
        "--unignore", metavar="CAMERA", nargs="+", default=[], help=_msg("aide_unignore"),
    )
    parser.add_argument(
        "--ignore-module", metavar="MODULE", nargs="+", default=[],
        help=_msg("aide_ignore_module"),
    )
    parser.add_argument(
        "--unignore-module", metavar="MODULE", nargs="+", default=[],
        help=_msg("aide_unignore_module"),
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
        print(_msg("cameras_en_sourdine"), ", ".join(state["ignored"]) or _msg("aucune"))

    if args.ignore_module or args.unignore_module:
        code = _sourdine_modules(args)
        if code:
            return code

    if args.test:
        popup(_msg("titre_test_alerte"), _msg("corps_test_alerte"))
        return 0

    # Ce programme contrôle l'état, rien de plus, conformément à son nom. La
    # répétition est une option commune à tous les verbes, pas un verbe.
    return runtime.repeter(
        lambda: un_tour(args, timezone),
        args.loop, journal,
    )


if __name__ == "__main__":
    raise SystemExit(main())
