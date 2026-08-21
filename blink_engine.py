"""Moteur de téléchargement : un passage USB puis cloud, en boucle ou une fois.

Extrait de blink2video.py à l'étape 8 (AUDIT-2026-08-13.md, section 20, 8.4).
Compose les modèles (blink_models) et le registre (blink_registre) ; ne
connaît rien de la CLI ni de la session, qui lui sont fournies toutes faites."""

import asyncio
import time
from pathlib import Path
from typing import NamedTuple

import runtime

from aiohttp import ClientError
from blinkpy.blinkpy import Blink
from blinkpy import livestream as _blinkpy_livestream

import merge_daily as md

import blink_models
import blink_registre


async def _recv_corrige(self):
    """Remplace BlinkLiveStream.recv (blinkpy/livestream.py) : lit les trames
    avec `readexactly` plutôt que `read`.

    `StreamReader.read(n)` rend dès que la moindre donnée est disponible, pas
    forcément les `n` octets demandés. Un paquet vidéo coupé entre deux
    segments TCP (fréquent, surtout sur les gros paquets) se lit alors en
    plusieurs morceaux ; le code amont traite le premier morceau incomplet
    comme une connexion morte et referme une session parfaitement saine,
    d'où les directs qui s'arrêtent après une poignée de secondes sans
    aucune erreur réseau réelle. `readexactly` attend la taille demandée et
    ne lève que sur une vraie fin de connexion.

    Correctif amont proposé mais pas encore publié sur PyPI :
    https://github.com/fronzbot/blinkpy/pull/1232 (confirmé avec un test de
    régression qui reproduit exactement ce symptôme). À retirer dès qu'une
    version de blinkpy l'intègre."""
    try:
        _blinkpy_livestream._LOGGER.debug("Starting copy from target to clients")
        while not self.target_reader.at_eof():
            try:
                data = await self.target_reader.readexactly(9)
            except asyncio.IncompleteReadError:
                _blinkpy_livestream._LOGGER.debug(
                    "Target closed before a full 9-byte header"
                )
                break

            msgtype = data[0]
            sequence = int.from_bytes(data[1:5], byteorder="big")
            payload_length = int.from_bytes(data[5:9], byteorder="big")
            _blinkpy_livestream._LOGGER.debug(
                "Received packet: msgtype=%d, sequence=%d, payload_length=%d",
                msgtype, sequence, payload_length,
            )

            if payload_length <= 0:
                _blinkpy_livestream._LOGGER.debug(
                    "Invalid payload length: %d", payload_length
                )
                continue

            try:
                data = await self.target_reader.readexactly(payload_length)
            except asyncio.IncompleteReadError:
                _blinkpy_livestream._LOGGER.debug(
                    "Target closed before a full payload"
                )
                break

            if msgtype != 0x00:
                _blinkpy_livestream._LOGGER.debug(
                    "Skipping unsupported msgtype %d", msgtype
                )
                continue

            if data[0] != 0x47:
                _blinkpy_livestream._LOGGER.debug(
                    "Skipping video payload missing 0x47 at start"
                )
                continue

            _blinkpy_livestream._LOGGER.debug("Sending %d bytes to clients", len(data))
            for writer in self.clients:
                if not writer.is_closing():
                    writer.write(data)
                    await writer.drain()

            await asyncio.sleep(0)
    except _blinkpy_livestream.ssl.SSLError as e:
        if e.reason != "APPLICATION_DATA_AFTER_CLOSE_NOTIFY":
            _blinkpy_livestream._LOGGER.exception("SSL error while receiving data")
    except Exception:
        _blinkpy_livestream._LOGGER.exception("Error while receiving data")
    finally:
        self.target_writer.close()
        _blinkpy_livestream._LOGGER.debug("Receiving was aborted, aborting sending")


_blinkpy_livestream.BlinkLiveStream.recv = _recv_corrige


class CloudResult(NamedTuple):
    """Bilan stable d'un passage cloud, sans confondre ses quatre issues."""

    downloaded: int = 0
    adopted: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def had_error(self) -> bool:
        return self.failed > 0

    @property
    def new_downloads(self) -> int:
        return self.downloaded


def hub_lock(owner: str, stale_after: int = 600):
    """Réserve le Sync Module. Conservé ici pour les appelants existants."""
    return runtime.verrou("hub", owner, stale_after)


BusyError = runtime.BusyError


class _HubCloud:
    """Module de substitution, quand seul le cloud répond."""

    def __init__(self, network_id):
        self.sync_id = network_id or "cloud"
        self.network_id = network_id or ""


async def download_clip(blink: Blink, clip, target: Path, overwrite: bool) -> str:
    """Prépare puis télécharge un clip, sans jamais le supprimer du hub.

    La suppression éventuelle (issue GitHub #1) est décidée par l'appelant,
    après coup : voir un_passage() et runtime.lire_suppression_auto()."""
    # md.valid_mp4, pas une simple taille non nulle (revue de code du
    # 0eab463, bug #4) : un fichier déjà présent mais corrompu (écriture
    # interrompue, disque en cause) était sinon tenu pour acquis et jamais
    # retéléchargé.
    if target.exists() and md.valid_mp4(target) and not overwrite:
        return "skipped"

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    partial.unlink(missing_ok=True)

    try:
        prepared = await clip.prepare_download(blink)
        if not prepared or not await clip.download_video(blink, str(partial)):
            return "failed"
        # I-15 : un fichier d'un octet passait ce contrôle et était inscrit
        # comme acquis. valid_mp4 lit la boîte ftyp, présente dès les
        # premiers octets d'un MP4 réel ; un flux tronqué ou une page
        # d'erreur HTML ne l'ont jamais.
        if not partial.exists() or not md.valid_mp4(partial):
            return "failed"
        partial.replace(target)
        return "downloaded"
    finally:
        partial.unlink(missing_ok=True)


async def traiter_cloud(blink: Blink, args, modules: list) -> CloudResult:
    """Inventorie, puis rapatrie, les clips que l'abonnement garde dans le cloud.

    Le compte répond ici, pas le module : aucune réservation du hub n'est donc
    nécessaire, et cette partie fonctionne même quand le module est occupé par
    un direct. Les fichiers rejoignent la même arborescence et le même registre
    que ceux de la clé, puisqu'un clip reste un clip."""
    clips = await blink_models.read_cloud_manifest(blink, args.since)
    if args.camera:
        clips = [c for c in clips if c.name.casefold() == args.camera.casefold()]
    if not clips:
        return CloudResult()

    print("\n=== CLOUD DE L'ABONNEMENT ===")
    # Le rapprochement se fait avec ce qui est déjà au registre, et non avec le
    # manifeste USB : celui-ci ne montre que ce que la clé contient encore,
    # alors que le registre garde la trace de tout ce qui a été rapatrié.
    output = args.output.resolve()
    state = blink_registre.load_download_state(output)
    connus = [
        blink_registre._ClipConnu(entree)
        for entree in state["clips"].values()
        if blink_registre._entree_acquise(output, entree)
    ]
    tombstones = [
        blink_registre._ClipConnu(entree)
        for entree in state["clips"].values()
        if isinstance(entree, dict) and entree.get("excluded")
    ]
    sans_tombstone = (
        blink_models.rapprocher(tombstones, clips)[0] if tombstones else list(clips)
    )
    # `sans_tombstone` contient les clips sans exclusion ; une décision explicite
    # reste donc prioritaire même lors d'un retéléchargement forcé.
    clips_autorises = sans_tombstone
    ignores_exclus = len(clips) - len(clips_autorises)
    if args.overwrite:
        inedits, doublons = clips_autorises, []
    else:
        inedits, doublons = blink_models.rapprocher(connus, clips_autorises)
    print(f"  {len(clips)} clip(s) dans le cloud, {len(doublons)} déjà acquis "
          f"par ailleurs, {len(inedits)} à rapatrier.")
    if args.command != "download" or not inedits:
        return CloudResult(skipped=len(doublons) + ignores_exclus)

    # Le registre attend un module pour former l'identité. Le cloud n'en
    # dépend pas : à défaut, le réseau du clip en tient lieu, ce qui suffit,
    # l'identité réelle restant la caméra et l'instant.
    # Issue GitHub #1 : par caméra, jamais globalement (voir un_passage()
    # pour le pendant USB).
    suppression_auto = runtime.lire_suppression_auto()
    downloaded = adopted = failed = 0
    for position, clip in enumerate(sorted(inedits, key=blink_models.clip_datetime_utc), start=1):
        sync = _HubCloud(clip.network_id)
        target = blink_models.target_path(output, clip, sync=sync, source="cloud")
        print(f"  [{position}/{len(inedits)}] {target.name}")
        _, entree_connue = blink_registre._trouver_entree(
            state, sync, clip, source="cloud",
        )
        if (
            entree_connue is None
            and target.exists()
            and target.is_file()
            and md.valid_mp4(target)
            and not args.overwrite
        ):
            blink_registre.remember_download(state, sync, args.hub or "cloud", clip, output,
                              target, source="cloud")
            blink_registre.save_download_state(output, state)
            adopted += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        partiel = target.with_suffix(target.suffix + ".part")
        partiel.unlink(missing_ok=True)
        try:
            if await clip.download_to(blink, partiel):
                partiel.replace(target)
                blink_registre.remember_download(state, sync, args.hub or "cloud", clip, output,
                                  target, source="cloud")
                blink_registre.save_download_state(output, state)
                downloaded += 1
                if clip.name in suppression_auto:
                    # target valide (download_to a deja verifie md.valid_mp4)
                    # avant qu'on retire la copie cloud.
                    if await clip.delete_video(blink):
                        print("    Supprimé du cloud (caméra en suppression "
                              "automatique).")
                    else:
                        print("    ! Échec de la suppression sur le cloud "
                              "(clip conservé là-bas).")
            else:
                failed += 1
                categorie, detail = getattr(
                    clip, "download_issue", None
                ) or ("média", "contenu indisponible")
                print(f"    Échec [{categorie}] : {detail}.")
        except (ClientError, OSError, asyncio.TimeoutError) as erreur:
            failed += 1
            print(f"    Échec [réseau] : {type(erreur).__name__}.")
        except Exception as erreur:  # Isoler un clip invalide des suivants.
            failed += 1
            print(f"    Échec [données] : {type(erreur).__name__}.")
        finally:
            partiel.unlink(missing_ok=True)

    resultat = CloudResult(
        downloaded=downloaded,
        adopted=adopted,
        skipped=len(doublons) + ignores_exclus,
        failed=failed,
    )
    print(
        f"  Terminé : {resultat.downloaded} téléchargé(s), "
        f"{resultat.adopted} adopté(s), {resultat.skipped} ignoré(s), "
        f"{resultat.failed} échec(s)."
    )
    return resultat


async def boucler(blink: Blink, args, modules: list) -> int:
    """Répète le passage si --loop est donné, en gardant la session ouverte.

    La session est ouverte une fois pour toutes : à cadence rapide, se
    reconnecter à chaque tour coûterait plus cher que le travail lui-même, et
    multiplierait les authentifications sans raison.

    Seul BusyError était intercepté auparavant : une panne HTTP, une erreur
    d'authentification ou un schéma Blink inattendu remontait alors hors de la
    boucle et tuait définitivement ce worker de fond (I-17). On élargit donc la
    capture à toute erreur du tour, en la journalisant, pour qu'un incident
    transitoire attende simplement le prochain passage au lieu d'exiger un
    redémarrage manuel. Ctrl+C (KeyboardInterrupt) et l'annulation de la tâche
    restent hors de `Exception` : ils continuent d'arrêter la boucle."""
    while True:
        echeance = time.monotonic() + (args.loop or 0) * 60
        try:
            # Un seul rapatriement à la fois, quelle que soit la source : deux
            # processus qui prennent le même clip écriraient le même fichier
            # partiel, et le premier renommage laisserait l'autre dans le vide.
            with runtime.verrou("download", "download", attente=30):
                try:
                    code = await un_passage(blink, args, modules)
                finally:
                    runtime.fin_travail()
        except runtime.BusyError as erreur:
            print(f"Téléchargement déjà en cours ({erreur}).")
            code = 0
        except Exception as erreur:
            print(f"Tour de téléchargement interrompu par une erreur, "
                  f"on réessaie au prochain : {erreur}")
            code = 1
        if not args.loop:
            return code
        # Échéance calculée depuis le début du tour, pas depuis sa fin : un
        # tour plus lent que la cadence ne doit pas décaler tous les suivants
        # d'autant (O-05). Un tour trop lent saute simplement son repos.
        await asyncio.sleep(max(0.0, echeance - time.monotonic()))


async def un_passage(blink: Blink, args, modules: list) -> int:
    """Un tour : la clé USB de chaque module, puis le cloud du compte."""
    had_error = False
    neufs_total = 0
    for name, sync in ([] if args.source == "cloud" else modules):
        print(f"\n=== STOCKAGE LOCAL : {name} ===")
        # B-06 : le manifeste comme le téléchargement parlent au Sync Module,
        # tout comme le direct et l'actualisation (serve.py). Sans ce verrou,
        # les deux pouvaient démarrer en même temps et se répondre « System is
        # busy » l'un l'autre au hasard du planificateur. attente=0 : un tour
        # de fond qui tombe sur un module occupé n'a pas à attendre, le
        # prochain passage suffit (I-17 garde la boucle vivante).
        try:
            with hub_lock(name):
                try:
                    clips = await blink_models.read_local_manifest(sync)
                except RuntimeError as error:
                    print(f"  Indisponible : {error}.")
                    had_error = True
                    continue

                clips = blink_models.filter_clips(clips, args.camera, args.since)
                blink_models.print_clip_summary(clips)
                # Issue GitHub #1 : par camera, jamais globalement, pour laisser
                # une camera encore incertaine en conservation pendant qu'une
                # autre, deja eprouvee, libere sa memoire tampon.
                suppression_auto = runtime.lire_suppression_auto()

                if args.command != "download" or not clips:
                    continue

                output = args.output.resolve()
                print(f"  Destination : {output}")
                state = blink_registre.load_download_state(output)
                pending = []
                adopted = 0
                index_registre = blink_registre._index_registre(state)
                correspondances = blink_registre._apparier_registre(
                    state, sync, clips, index_registre,
                )
                for indice_clip, clip in enumerate(clips):
                    target = blink_models.target_path(output, clip, sync=sync, source="usb")
                    _, entree_connue = correspondances.get(
                        indice_clip, (None, None),
                    )
                    if isinstance(entree_connue, dict) and entree_connue.get("excluded"):
                        continue
                    if (
                        isinstance(entree_connue, dict)
                        and blink_registre._entree_acquise(output, entree_connue)
                        and not args.overwrite
                    ):
                        continue
                    if (
                        entree_connue is None
                        and target.exists()
                        and target.is_file()
                        and md.valid_mp4(target)
                        and not args.overwrite
                    ):
                        blink_registre.remember_download(state, sync, name, clip, output, target)
                        adopted += 1
                        continue
                    pending.append(clip)

                blink_registre.save_download_state(output, state)
                already_downloaded = len(clips) - len(pending) - adopted
                print(
                    f"  Incrémental : {len(pending)} nouveau(x), "
                    f"{already_downloaded + adopted} déjà acquis."
                )
                if not pending:
                    continue

                downloaded = skipped = failed = 0
                for position, clip in enumerate(pending, start=1):
                    target = blink_models.target_path(output, clip, sync=sync, source="usb")
                    print(f"  [{position}/{len(pending)}] {target.name}")
                    runtime.travail("Téléchargement des clips", position - 1, len(pending),
                                    cle="phase.download_clips")
                    try:
                        result = await download_clip(blink, clip, target, args.overwrite)
                    except Exception as error:  # Continuer avec les autres clips.
                        print(f"    Échec : {type(error).__name__}: {error}")
                        result = "failed"

                    if result == "downloaded":
                        downloaded += 1
                        blink_registre.remember_download(state, sync, name, clip, output, target)
                        blink_registre.save_download_state(output, state)
                        if clip.name in suppression_auto:
                            # target valide (download_clip ne renvoie
                            # "downloaded" qu'après md.valid_mp4) : la copie
                            # locale existe déjà avant qu'on libère le hub.
                            if await clip.delete_video(blink):
                                print("    Supprimé du Sync Module (caméra en "
                                      "suppression automatique).")
                            else:
                                print("    ! Échec de la suppression sur le "
                                      "Sync Module (clip conservé là-bas).")
                    elif result == "skipped":
                        skipped += 1
                        if target.exists() and md.valid_mp4(target):
                            blink_registre.remember_download(state, sync, name, clip, output, target)
                            blink_registre.save_download_state(output, state)
                    else:
                        failed += 1
                        print("    Échec du téléchargement après plusieurs tentatives.")

                print(
                    f"  Terminé : {downloaded} téléchargé(s), "
                    f"{skipped} déjà présent(s), {failed} échec(s)."
                )
                had_error = had_error or failed > 0
                neufs_total += downloaded
        except runtime.BusyError as error:
            print(f"  Module occupé (direct ou actualisation en cours) : {error}.")
            had_error = True
            continue

    if args.source != "usb":
        resultat_cloud = await traiter_cloud(blink, args, modules)
        had_error = resultat_cloud.had_error or had_error
        neufs_total += resultat_cloud.new_downloads

    if args.command == "download":
        # Ligne de synthèse, toutes sources confondues.
        print(f"\nNouveaux clips : {neufs_total}")
        runtime.marquer("download")
        if neufs_total:
            # Le verbe qui ramène est celui qui annonce : « watch » regarde les
            # caméras, il n'a pas à parler des clips.
            pluriel = "s" if neufs_total > 1 else ""
            # Suit la langue de la page (runtime.lire_langue(), voir tray.py) :
            # une notification système reste visible même fenêtre fermée, elle
            # doit parler la même langue que ce que l'utilisateur a choisi.
            if runtime.lire_langue() == "en":
                corps = (f"{neufs_total} new clip{'s' if neufs_total > 1 else ''} "
                         "downloaded. Click to open.")
            else:
                corps = (f"{neufs_total} nouveau{'x' if neufs_total > 1 else ''} "
                         f"clip{pluriel} récupéré{pluriel}. Cliquez pour ouvrir.")
            runtime.toast("Blink", corps, url="http://127.0.0.1:8765/")

    return 1 if had_error else 0
