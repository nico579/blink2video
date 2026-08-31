"""Modèles et adaptateurs : clips locaux et cloud, identité, chemins.

Extrait de blink2video.py à l'étape 8 (AUDIT-2026-08-13.md, section 20, 8.2).
Ce fichier ne connaît ni le registre (persistance), ni la session (auth) :
il transforme les objets de blinkpy et du cloud en une forme commune que le
reste du programme manipule sans se soucier de la source."""

from __future__ import annotations

import bisect
import contextlib
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

# O-06/8.7/8.8 : blink_registre.py importe ce module pour ses seules fonctions
# de corrélation, y compris pour des verbes (stop, open) qui n'ont besoin ni
# de blinkpy ni d'aiohttp. `Blink` n'est utilisé ici qu'en annotation de type
# (jamais instancié) : le report sous TYPE_CHECKING, combiné à
# `from __future__ import annotations`, évite d'exiger blinkpy installé
# seulement pour importer ce fichier.
if TYPE_CHECKING:
    from blinkpy.blinkpy import Blink

# I-15 : réutilise la même vérification de boîte ftyp que merge_daily plutôt
# que d'en écrire une seconde copie qui dériverait tôt ou tard.
import merge_daily as md


def _premiere_valeur(*valeurs):
    """Première valeur API présente, sans confondre ``False`` et l'absence."""
    return next((valeur for valeur in valeurs if valeur not in (None, "")), None)


def _booleen_api(valeur) -> bool:
    """Normalise les booléens JSON et leurs rares formes textuelles."""
    if isinstance(valeur, str):
        return valeur.casefold().strip() in {
            "1", "true", "yes", "active", "enabled",
        }
    return bool(valeur)


def _objet_emballe(entree, cle: str) -> tuple[dict, dict]:
    """Retourne l'objet homescreen et son enveloppe éventuelle."""
    enveloppe = entree if isinstance(entree, dict) else {}
    objet = enveloppe.get(cle)
    return (objet if isinstance(objet, dict) else enveloppe), enveloppe


def _noms_reseaux(homescreen: dict) -> dict[str, str]:
    noms = {}
    for entree in homescreen.get("networks") or []:
        reseau, enveloppe = _objet_emballe(entree, "network")
        identifiant = _premiere_valeur(
            reseau.get("id"), reseau.get("network_id"),
            enveloppe.get("id"), enveloppe.get("network_id"),
        )
        nom = _premiere_valeur(reseau.get("name"), enveloppe.get("name"))
        if identifiant is not None and nom is not None:
            noms[str(identifiant)] = str(nom).strip()
    return noms


def _noms_cameras(homescreen: dict, network_id) -> list[str]:
    """Noms du réseau, nécessaires au parseur de manifeste de blinkpy."""
    noms = []
    for groupe in ("cameras", "owls", "doorbells"):
        for entree in homescreen.get(groupe) or []:
            appareil, enveloppe = _objet_emballe(entree, "device")
            reseau = _premiere_valeur(
                appareil.get("network_id"), appareil.get("network"),
                enveloppe.get("network_id"), enveloppe.get("network"),
            )
            nom = _premiere_valeur(appareil.get("name"), enveloppe.get("name"))
            if nom is not None and str(reseau) == str(network_id):
                noms.append(str(nom).strip())
    return noms


def _modules_homescreen(blink: Blink) -> list:
    """Adapte les vrais Sync Modules annoncés par le homescreen.

    blinkpy 0.25.9 découvre encore les réseaux et modules avec deux endpoints
    historiques. Un appareil autonome (Mini/Doorbell) peut alors occuper
    ``blink.sync`` avec son propre identifiant, tandis que le véritable module
    de stockage est pourtant présent dans ``homescreen["sync_modules"]``. Le
    manifeste exige l'ID de ce dernier. On construit donc, sans appel réseau,
    la même surface ``BlinkSyncModule`` à partir de la réponse déjà reçue.
    """
    homescreen = getattr(blink, "homescreen", None)
    if not isinstance(homescreen, dict):
        return []
    entrees = homescreen.get("sync_modules")
    if not isinstance(entrees, (list, tuple)):
        return []

    existants = list((getattr(blink, "sync", None) or {}).items())
    noms_reseaux = _noms_reseaux(homescreen)
    modules = []
    identites_vues = set()

    for entree in entrees:
        module, enveloppe = _objet_emballe(entree, "sync_module")
        sync_id = _premiere_valeur(
            module.get("id"), module.get("sync_module_id"),
            enveloppe.get("id"), enveloppe.get("sync_module_id"),
        )
        network_id = _premiere_valeur(
            module.get("network_id"), module.get("network"),
            enveloppe.get("network_id"), enveloppe.get("network"),
        )
        identite = (str(network_id), str(sync_id))
        if sync_id is None or network_id is None or identite in identites_vues:
            continue
        identites_vues.add(identite)

        nom_module = _premiere_valeur(module.get("name"), enveloppe.get("name"))
        nom = noms_reseaux.get(str(network_id)) or str(
            nom_module or network_id
        ).strip()

        # Réutiliser l'objet normal de blinkpy quand il porte déjà le bon ID.
        # Un objet sans ID est aussi le BlinkSyncModule créé avant l'échec de
        # l'ancien endpoint. En revanche, un ID différent sur le même réseau
        # appartient à une Mini/Doorbell : ne jamais le transformer en XR.
        sync = next(
            (objet for _cle, objet in existants
             if str(getattr(objet, "network_id", "")) == str(network_id)
             and str(getattr(objet, "sync_id", "")) == str(sync_id)),
            None,
        )
        if sync is None:
            sync = next(
                (objet for _cle, objet in existants
                 if str(getattr(objet, "network_id", "")) == str(network_id)
                 and getattr(objet, "sync_id", None) in (None, "")),
                None,
            )
        if sync is None:
            # Import tardif : les commandes hors-ligne (stop/open/merge) doivent
            # continuer à importer blink_models sans exiger blinkpy.
            from blinkpy.sync_module import BlinkSyncModule

            sync = BlinkSyncModule(blink, nom, str(network_id), [])

        sync.sync_id = sync_id
        sync.network_id = str(network_id)
        sync.name = nom
        sync.summary = module
        sync.serial = _premiere_valeur(
            module.get("serial"), enveloppe.get("serial"),
            getattr(sync, "serial", None),
        )
        sync.status = _premiere_valeur(
            module.get("status"), enveloppe.get("status"),
            getattr(sync, "status", None),
        )
        sync._version = _premiere_valeur(
            module.get("fw_version"), enveloppe.get("fw_version"),
            getattr(sync, "_version", None),
        )

        stockage = sync._local_storage
        enabled = _premiere_valeur(
            module.get("local_storage_enabled"),
            enveloppe.get("local_storage_enabled"),
        )
        compatible = _premiere_valeur(
            module.get("local_storage_compatible"),
            enveloppe.get("local_storage_compatible"),
        )
        status = _premiere_valeur(
            module.get("local_storage_status"),
            enveloppe.get("local_storage_status"),
        )
        if enabled is not None:
            stockage["enabled"] = _booleen_api(enabled)
        if status is not None:
            stockage["status"] = _booleen_api(status)
        if compatible is not None:
            stockage["compatible"] = _booleen_api(compatible)
        elif stockage.get("enabled") or stockage.get("status"):
            # Un stockage déclaré actif établit sa compatibilité même si la
            # réponse API a omis le champ redondant ``compatible``.
            stockage["compatible"] = True

        from blinkpy.helpers.util import to_alphanumeric

        camera_names = set(_noms_cameras(homescreen, network_id))
        # Replis pour les variantes de homescreen qui omettent un groupe mais
        # que blinkpy a déjà réussi à instancier par une autre route.
        for camera_name, camera in (getattr(blink, "cameras", None) or {}).items():
            camera_network = getattr(camera, "network_id", None)
            if camera_network is None:
                camera_network = getattr(
                    getattr(camera, "sync", None), "network_id", None,
                )
            if str(camera_network) == str(network_id):
                camera_names.add(str(camera_name).strip())
        for _existing_name, existing in existants:
            if str(getattr(existing, "network_id", "")) != str(network_id):
                continue
            camera_names.update(
                str(camera_name).strip()
                for camera_name in (getattr(existing, "cameras", None) or {})
            )

        for camera_name in camera_names:
            sync._names_table[to_alphanumeric(camera_name)] = camera_name

        alias = {nom}
        if nom_module is not None:
            alias.add(str(nom_module).strip())
        sync._blink2video_hub_names = alias
        modules.append((nom, sync))

    return modules


def select_sync_modules(blink: Blink, requested_name: str | None):
    """Sélectionne tous les hubs, ou celui demandé par son nom."""
    # Le homescreen est l'inventaire moderne et contient l'ID matériel requis
    # par l'API de stockage local. Repli sur blink.sync pour les anciennes
    # réponses et pour conserver le comportement historique des tests/clients.
    modules = _modules_homescreen(blink) or list(
        (getattr(blink, "sync", None) or {}).items()
    )
    if not requested_name:
        return modules

    demande = requested_name.casefold().strip()
    selected = [
        (name, sync)
        for name, sync in modules
        if demande in {
            str(alias).casefold().strip()
            for alias in getattr(sync, "_blink2video_hub_names", {name})
        }
    ]
    if not selected:
        available = ", ".join(name for name, _ in modules) or "aucun"
        raise ValueError(
            f"Sync Module introuvable : {requested_name!r}. Disponible(s) : {available}."
        )
    return selected


async def read_local_manifest(sync) -> list:
    """Demande au Sync Module la liste à jour de ses clips locaux."""
    import asyncio

    storage = sync._local_storage  # blinkpy n'expose pas encore d'accesseur public.
    if not storage.get("compatible"):
        raise RuntimeError("ce hub n'est pas compatible avec le stockage local")
    if not storage.get("enabled"):
        raise RuntimeError("le stockage local n'est pas activé sur ce hub")
    if not sync.local_storage:
        raise RuntimeError(
            "le stockage local n'est pas actif (clé USB ou carte microSD "
            "absente/non reconnue, ou clips enregistrés dans le cloud)"
        )

    print("  Lecture du manifeste du stockage local...")
    # Le Sync Module ne traite qu'une commande à la fois et répond « System is
    # busy » (code 307) tant qu'il n'a pas fini la précédente : un direct qui
    # vient de se fermer, ou une autre demande de manifeste, suffisent. Ce
    # refus est temporaire, pas une panne, d'où la reprise avec une attente qui
    # s'allonge plutôt qu'un abandon immédiat.
    delays = (3, 8, 15, 25)
    for attempt, delay in enumerate((*delays, None), start=1):
        if await sync.update_local_storage_manifest():
            return list(storage["manifest"])
        if delay is None:
            break
        print(f"  Module occupé, nouvelle tentative dans {delay} s "
              f"({attempt}/{len(delays)})...")
        await asyncio.sleep(delay)

    raise RuntimeError(
        "Blink n'a pas renvoyé le manifeste du stockage local après "
        f"{len(delays) + 1} tentatives (module resté occupé)"
    )


class CloudClip:
    """Un clip du cloud, présenté comme ceux du stockage local.

    Même surface que les objets de blinkpy : un nom de caméra, un instant, un
    identifiant. Le reste de la chaîne, identité, nom de fichier, registre,
    normalisation, ignore donc d'où vient l'enregistrement, et c'est le but :
    une seule archive, quelle que soit la source."""

    def __init__(self, entree: dict):
        self.raw = entree
        self.name = str(entree.get("device_name") or "").strip() or "camera"
        self.id = entree.get("id")
        self.created_at = dt.datetime.fromisoformat(str(entree["created_at"]))
        self.address = entree.get("media")
        self.network_id = entree.get("network_id")
        self.device_id = (
            entree.get("device_id") or entree.get("camera_id")
        )
        self.download_issue = None

    async def download_to(self, blink: Blink, target: Path) -> bool:
        """Diffuse le média vers le fichier temporaire choisi par l'appelant."""
        self.download_issue = None
        target.unlink(missing_ok=True)
        if not self.address:
            self.download_issue = ("données", "adresse du média absente")
            return False
        try:
            reponse = await blink.do_http_get(self.address)
            if reponse is None:
                self.download_issue = ("réseau", "aucune réponse du service Blink")
                return False
            statut = getattr(reponse, "status", None)
            if not isinstance(statut, int) or not 200 <= statut < 300:
                detail = f"statut HTTP {statut}" if isinstance(statut, int) else "statut HTTP absent"
                self.download_issue = ("HTTP", f"réponse refusée ({detail})")
                with contextlib.suppress(Exception):
                    await reponse.read()
                return False

            target.parent.mkdir(parents=True, exist_ok=True)
            recus = 0
            flux = getattr(reponse, "content", None)
            iter_chunked = getattr(flux, "iter_chunked", None)
            if callable(iter_chunked):
                # aiohttp décompresse éventuellement à la volée ; la taille
                # du bloc borne donc la mémoire sans présumer du Content-Length.
                with target.open("wb") as destination:
                    async for bloc in iter_chunked(256 * 1024):
                        if bloc:
                            destination.write(bloc)
                            recus += len(bloc)
            else:
                # Compatibilité avec les réponses minimales de blinkpy et les
                # doubles de tests qui n'exposent que ``read()``.
                contenu = await reponse.read()
                with target.open("wb") as destination:
                    destination.write(contenu)
                recus = len(contenu)

            if recus == 0:
                self.download_issue = ("HTTP", "réponse vide")
                return False

            entetes = getattr(reponse, "headers", None)
            encodage = entetes.get("Content-Encoding") if entetes is not None else None
            longueur = entetes.get("Content-Length") if entetes is not None else None
            if not encodage and longueur is not None:
                try:
                    attendu = int(longueur)
                except (TypeError, ValueError):
                    attendu = None
                if attendu is not None and attendu >= 0 and recus != attendu:
                    self.download_issue = (
                        "données",
                        f"réponse tronquée ({recus} octets reçus sur {attendu})",
                    )
                    return False

            # Contrôle structurel peu coûteux ici ; l'appelant effectue la
            # lecture approfondie avant le renommage atomique et la suppression.
            if not md.valid_mp4(target):
                self.download_issue = (
                    "données", "corps reçu, mais MP4 incomplet ou invalide",
                )
                return False
            return True
        except Exception as erreur:
            categorie = "écriture" if isinstance(erreur, OSError) else "réseau"
            self.download_issue = (categorie, type(erreur).__name__)
            return False
        finally:
            if self.download_issue is not None:
                with contextlib.suppress(OSError):
                    target.unlink(missing_ok=True)

    async def delete_video(self, blink: Blink) -> bool:
        """Supprime ce clip du cloud de l'abonnement (issue GitHub #1).

        Endpoint non documenté par blinkpy (aucune méthode équivalente côté
        cloud), identifié et vérifié manuellement contre le compte réel :
        POST /api/v1/accounts/{account_id}/media/delete avec la liste des
        identifiants à supprimer. Le code de retour 711 est la confirmation
        du service ; tout le reste (erreur réseau, code différent) compte
        comme un échec plutôt qu'un succès supposé."""
        url = f"{blink.urls.base_url}/api/v1/accounts/{blink.account_id}/media/delete"
        try:
            resultat = await blink.auth.query(
                url=url, headers=blink.auth.header, reqtype="post",
                data=json.dumps({"media_list": [self.id]}),
            )
        except Exception:
            return False
        return isinstance(resultat, dict) and resultat.get("code") == 711


# get_videos_metadata (blinkpy) boucle `for page in range(1, stop)` et
# s'arrête déjà de lui-même dès qu'une page revient vide - `stop` n'est donc
# qu'un plafond de sécurité, pas la pagination réelle. Fixé à 20 (19 pages,
# ~25 éléments chacune, soit environ 475 clips), il agissait en pratique
# comme une troncature silencieuse : un compte actif multi-caméras peut
# dépasser ce volume en 30 jours, les clips les plus anciens de la fenêtre
# n'étant alors jamais vus (revue de code du 0eab463, bug #6). 400 laisse une
# marge considérable (~10 000 clips) pour un usage réel quelconque, tout en
# restant une valeur finie plutôt que de retirer toute protection.
PLAFOND_PAGES_CLOUD = 400


async def read_cloud_manifest(blink: Blink, since_days: int | None) -> list:
    """Inventaire des clips conservés dans le cloud de l'abonnement Blink.

    Distinct du manifeste local, et indépendant du Sync Module : c'est le compte
    qui répond, pas le module. Un abonnement ne couvrant qu'une partie des
    caméras, les deux inventaires ne se recouvrent que partiellement, d'où le
    rapprochement fait plus loin plutôt qu'un choix de source."""
    jours = 30 if since_days is None else since_days
    if jours < 0:
        raise ValueError("le nombre de jours doit être positif ou nul")
    depuis = dt.datetime.now() - dt.timedelta(days=jours)
    entrees = await blink.get_videos_metadata(
        since=depuis.strftime("%Y/%m/%d %H:%M:%S"), stop=PLAFOND_PAGES_CLOUD
    )
    if isinstance(entrees, (list, tuple)) and len(entrees) >= (PLAFOND_PAGES_CLOUD - 3) * 20:
        # Compte proche du plafond : impossible de savoir depuis ici si la
        # dernière page était vide (fin réelle) ou si le plafond a coupé une
        # pagination encore active - signalé plutôt que tranché en silence.
        print(f"  ! [données] Manifeste cloud proche du plafond de sécurité "
              f"({PLAFOND_PAGES_CLOUD} pages) : des clips plus anciens dans "
              f"la fenêtre demandée ont peut-être été omis.")
    if entrees is None:
        return []
    if not isinstance(entrees, (list, tuple)):
        print("  ! [données] Manifeste cloud ignoré : racine JSON inattendue.")
        return []
    clips = []
    invalides = 0
    for entree in entrees:
        if not isinstance(entree, dict):
            invalides += 1
            continue
        # Un média supprimé reste listé, marqué comme tel ; un média partiel est
        # un enregistrement encore en cours d'écriture côté Blink.
        if entree.get("deleted") or entree.get("partial"):
            continue
        if str(entree.get("type") or "video") != "video":
            continue
        try:
            clips.append(CloudClip(entree))
        except (KeyError, TypeError, ValueError):
            invalides += 1
    if invalides:
        print(f"  ! [données] {invalides} entrée(s) cloud invalide(s) ignorée(s).")
    return clips


def _identifiant_reseau(clip, sync=None) -> str:
    """Retourne le réseau immuable quand l'API l'expose."""
    valeur = getattr(clip, "network_id", None)
    if valeur in (None, "") and sync is not None:
        valeur = getattr(sync, "network_id", None)
    return str(valeur or "")


def _identifiant_camera(clip) -> str:
    """Retourne l'identifiant matériel, absent des objets USB blinkpy 0.25."""
    for attribut in ("device_id", "camera_id"):
        valeur = getattr(clip, attribut, None)
        if valeur not in (None, ""):
            return str(valeur)
    brut = getattr(clip, "raw", None)
    if isinstance(brut, dict):
        for cle in ("device_id", "camera_id"):
            if brut.get(cle) not in (None, ""):
                return str(brut[cle])
    return ""


def _meme_camera(gauche, droite, sync_gauche=None, sync_droite=None) -> bool:
    """Compare deux caméras sans confondre deux réseaux explicites."""
    reseau_gauche = _identifiant_reseau(gauche, sync_gauche)
    reseau_droite = _identifiant_reseau(droite, sync_droite)
    if reseau_gauche and reseau_droite and reseau_gauche != reseau_droite:
        return False

    camera_gauche = _identifiant_camera(gauche)
    camera_droite = _identifiant_camera(droite)
    if camera_gauche and camera_droite:
        return camera_gauche == camera_droite

    # blinkpy 0.25 n'expose ni device_id ni network_id sur certains objets USB.
    # Dans ce seul cas, le nom API original (jamais le nom de chemin assaini)
    # reste le meilleur signal disponible pour rapprocher USB et cloud.
    return str(gauche.name).casefold() == str(droite.name).casefold()


def _apparier_evenements(locaux: list, distants: list, tolerance: int = 2, *,
                         priorites_locaux=None, compatibles=None) -> list:
    """Retourne un appariement maximal ``(local, distant)`` déterministe."""
    if tolerance < 0 or not locaux or not distants:
        return []

    # L'index temporel réduit le graphe aux seuls voisins dans la fenêtre. Les
    # listes d'adjacence sont ensuite ordonnées par écart puis par index afin
    # que deux exécutions sur les mêmes manifestes rendent les mêmes paires.
    marge = dt.timedelta(seconds=tolerance)
    locaux_ordonnes = sorted(
        (clip_datetime_utc(local), indice)
        for indice, local in enumerate(locaux)
    )
    instants_locaux = [instant for instant, _ in locaux_ordonnes]
    priorites = priorites_locaux or [0] * len(locaux)
    meme_camera = compatibles or _meme_camera
    voisinages = [[] for _ in distants]
    for indice_distant, distant in enumerate(distants):
        instant_cloud = clip_datetime_utc(distant)
        debut = bisect.bisect_left(instants_locaux, instant_cloud - marge)
        fin = bisect.bisect_right(instants_locaux, instant_cloud + marge)
        for position in range(debut, fin):
            instant_local, indice_local = locaux_ordonnes[position]
            local = locaux[indice_local]
            if not meme_camera(local, distant):
                continue
            ecart = abs((instant_local - instant_cloud).total_seconds())
            voisinages[indice_distant].append(
                (priorites[indice_local], ecart, indice_local)
            )
        voisinages[indice_distant].sort()

    infini = len(locaux) + len(distants) + 1
    ordre_distants = sorted(
        range(len(distants)),
        key=lambda indice: (
            voisinages[indice][0][:2] if voisinages[indice]
            else (math.inf, math.inf),
            indice,
        ),
    )
    paire_distante = [-1] * len(distants)
    paire_locale = [-1] * len(locaux)

    def niveaux():
        distances = [infini] * len(distants)
        file = []
        for indice_distant in ordre_distants:
            if paire_distante[indice_distant] < 0:
                distances[indice_distant] = 0
                file.append(indice_distant)

        profondeur_libre = infini
        position = 0
        while position < len(file):
            indice_distant = file[position]
            position += 1
            profondeur_suivante = distances[indice_distant] + 1
            if profondeur_suivante > profondeur_libre:
                continue
            for _, _, indice_local in voisinages[indice_distant]:
                autre_distant = paire_locale[indice_local]
                if autre_distant < 0:
                    profondeur_libre = min(profondeur_libre, profondeur_suivante)
                elif (
                    profondeur_suivante < profondeur_libre
                    and distances[autre_distant] == infini
                ):
                    distances[autre_distant] = profondeur_suivante
                    file.append(autre_distant)
        return distances, profondeur_libre

    def augmenter(racine, distances, profondeur_libre, positions):
        # Version itérative du parcours de Hopcroft-Karp : aucun risque de
        # dépasser la profondeur de récursion sur un grand registre.
        pile = [racine]
        parents = {}
        while pile:
            indice_distant = pile[-1]
            avance = False
            while positions[indice_distant] < len(voisinages[indice_distant]):
                _, _, indice_local = voisinages[indice_distant][
                    positions[indice_distant]
                ]
                positions[indice_distant] += 1
                autre_distant = paire_locale[indice_local]
                if autre_distant < 0:
                    if distances[indice_distant] + 1 != profondeur_libre:
                        continue
                    courant, local_libre = indice_distant, indice_local
                    while True:
                        paire_distante[courant] = local_libre
                        paire_locale[local_libre] = courant
                        if courant == racine:
                            return True
                        courant, local_libre = parents[courant]
                elif (
                    distances[autre_distant] == distances[indice_distant] + 1
                    and autre_distant not in parents
                ):
                    parents[autre_distant] = (indice_distant, indice_local)
                    pile.append(autre_distant)
                    avance = True
                    break
            if not avance:
                distances[indice_distant] = infini
                pile.pop()
        return False

    while True:
        distances, profondeur_libre = niveaux()
        if profondeur_libre == infini:
            break
        positions = [0] * len(distants)
        for indice_distant in ordre_distants:
            if paire_distante[indice_distant] < 0:
                augmenter(indice_distant, distances, profondeur_libre, positions)

    return sorted(
        (indice_local, indice_distant)
        for indice_distant, indice_local in enumerate(paire_distante)
        if indice_local >= 0
    )


def rapprocher(locaux: list, cloud: list, tolerance: int = 2) -> tuple:
    """Sépare les clips cloud inédits de ceux déjà offerts localement.

    Une même détection peut être écrite des deux côtés lorsque l'abonnement
    couvre une caméra dont le stockage local fonctionne aussi. L'identité reste
    la caméra et l'instant, mais les deux sources horodatent l'événement
    séparément : quelques secondes d'écart sont possibles, d'où la tolérance.
    Sans elle, le même enregistrement serait rapatrié deux fois et apparaîtrait
    en double dans la journalière.

    Fonction pure, éprouvée par la suite dédiée sans compte Blink."""
    paires = _apparier_evenements(locaux, cloud, tolerance)
    cloud_pris = {indice_cloud for _, indice_cloud in paires}

    doublons = [clip for indice, clip in enumerate(cloud) if indice in cloud_pris]
    inedits = [clip for indice, clip in enumerate(cloud) if indice not in cloud_pris]
    return inedits, doublons


def clip_datetime_utc(clip) -> dt.datetime:
    """Normalise l'horodatage Blink en UTC."""
    created = clip.created_at
    if created.tzinfo is None:
        return created.replace(tzinfo=dt.timezone.utc)
    return created.astimezone(dt.timezone.utc)


def filter_clips(clips: list, camera: str | None, since_days: int | None) -> list:
    """Applique les filtres demandés, puis trie du plus ancien au plus récent."""
    selected = clips
    if camera:
        selected = [clip for clip in selected if clip.name.casefold() == camera.casefold()]
    if since_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)
        selected = [clip for clip in selected if clip_datetime_utc(clip) >= cutoff]
    return sorted(selected, key=clip_datetime_utc)


def reported_bytes(value) -> int:
    """Convertit en octets la taille du manifeste Blink, exprimée en Kio."""
    try:
        return int(value) * 1024
    except (TypeError, ValueError):
        return 0


def human_size(size: int) -> str:
    """Affiche une taille lisible."""
    amount = float(size)
    for unit in ("o", "Kio", "Mio", "Gio"):
        if amount < 1024 or unit == "Gio":
            return f"{amount:.0f} {unit}" if unit == "o" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} o"


# Alias, pas une copie : trois versions quasi identiques de ce nettoyage
# avaient dérivé (revue de code du 0eab463, bug #5). md.safe_name (celle-ci
# à l'origine) reste la référence unique - voir sa docstring.
safe_name = md.safe_name


def target_path(output: Path, clip, sync=None, source: str | None = None) -> Path:
    """Construit un chemin sûr ; ce chemin n'est jamais l'identité métier.

    Le suffixe porte une courte empreinte des valeurs API avant assainissement.
    Ainsi deux noms/identifiants qui deviennent le même composant de chemin, ou
    deux hubs qui réemploient le même numéro de manifeste, restent distincts.
    ``sync_id`` ne concerne que l'USB : dans le cloud, ``network_id`` désigne la
    source et le pseudo-module utilisé par l'appelant ne doit pas modifier le
    chemin.
    """
    created = clip_datetime_utc(clip)
    camera = safe_name(clip.name)
    month = created.strftime("%Y-%m")
    identifiant = safe_name(str(clip.id))[:40]
    provenance = source or (
        "cloud" if isinstance(clip, CloudClip) or hasattr(clip, "download_to")
        else "usb"
    )
    empreinte = json.dumps(
        [
            provenance,
            _identifiant_reseau(clip, sync),
            str(getattr(sync, "sync_id", "")) if provenance == "usb" else "",
            str(clip.name),
            str(getattr(clip, "id", "")),
            created.isoformat(),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    suffixe = hashlib.sha256(empreinte.encode("utf-8")).hexdigest()[:12]
    filename = (
        f"{created:%Y-%m-%d_%H-%M-%SZ}_{camera}_{identifiant}_{suffixe}.mp4"
    )
    return output / camera / month / filename


def print_clip_summary(clips: list) -> None:
    """Affiche un résumé du manifeste, sans URL ni donnée sensible."""
    total_size = sum(reported_bytes(clip.size) for clip in clips)
    print(f"  {len(clips)} clip(s), volume annoncé : environ {human_size(total_size)}")
    if clips:
        first = clip_datetime_utc(clips[0]).astimezone()
        last = clip_datetime_utc(clips[-1]).astimezone()
        print(f"  Période : {first:%Y-%m-%d %H:%M:%S %Z} -> {last:%Y-%m-%d %H:%M:%S %Z}")
        cameras = sorted({clip.name for clip in clips}, key=str.casefold)
        print(f"  Caméra(s) : {', '.join(cameras)}")
