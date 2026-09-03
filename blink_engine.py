"""Moteur de téléchargement : un passage USB puis cloud, en boucle ou une fois.

Extrait de blink2video.py à l'étape 8 (AUDIT-2026-08-13.md, section 20, 8.4).
Compose les modèles (blink_models) et le registre (blink_registre) ; ne
connaît rien de la CLI ni de la session, qui lui sont fournies toutes faites."""

import asyncio
import copy
import datetime as dt
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
        premier_video = True
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

            if premier_video:
                premier_video = False
                # Diagnostic seul, jamais critique : un test unitaire de ce
                # recv() (test_blink2video_audit.py) construit un
                # BlinkLiveStream nu via __new__, sans `camera` - une
                # exception ici (AttributeError sur self.camera, ou
                # n'importe quoi d'autre) ne doit surtout pas remonter au
                # except englobant, qui couperait alors tout le flux vidéo
                # juste après la première image (constaté en vrai : ce bloc
                # non protégé faisait échouer client.write() en silence).
                # Marque la fin de la part hors de portée de blink2video
                # (ticket cloud, réveil radio de la caméra, poignée de main
                # du relais). À comparer aux horodatages de
                # _journal_direct (serve.py, meme prefixe "[direct]" pour
                # le seul délai qui nous appartient (proxy local +
                # spawn/analyse/remux ffmpeg). Écrit aussi dans direct.log
                # (runtime.ajouter_ligne) : sous pythonw, un print() seul ne
                # va nulle part d'observable.
                try:
                    horodatage = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    ligne = (
                        f"[direct] {horodatage} {self.camera.name} : "
                        f"premier octet vidéo du relais"
                    )
                    print(ligne, flush=True)
                    runtime.ajouter_ligne("direct.log", ligne)
                except Exception:
                    pass

            _blinkpy_livestream._LOGGER.debug("Sending %d bytes to clients", len(data))
            for writer in self.clients:
                if not writer.is_closing():
                    # Une déconnexion d'UN client pendant drain() ne doit pas
                    # remonter jusqu'au except/finally de recv() : ça fermerait
                    # target_writer et couperait le direct pour tous les autres
                    # clients encore là. join() (blinkpy/livestream.py) reste
                    # seul responsable de retirer ce writer de self.clients.
                    try:
                        writer.write(data)
                        await writer.drain()
                    except OSError:
                        _blinkpy_livestream._LOGGER.debug(
                            "Client disconnected during drain, skipping it"
                        )

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

# Ne pas remplacer BlinkLiveStream.auth : le relais vidéo Blink présente un
# certificat auto-signé, et blinkpy lui applique donc un contexte TLS CERT_NONE
# limité à cette connexion. Une validation CA classique a été tentée dans
# v0.10.6 ; elle a cassé tous les directs avec CERTIFICATE_VERIFY_FAILED avant
# le premier octet. Les appels API Blink ordinaires restent, eux, strictement
# validés par blink_auth.contexte_tls().


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


class _ProgressionTelechargement:
    """Compteur unique d'un passage, partagé par tous ses modules et sources.

    Un élément représente un clip logique. Quand le même événement existe sur
    l'USB et dans le cloud, le téléchargement cloud est son repli et ne gonfle
    donc pas le total. Les lignes ``[n/total]`` alimentent le flux SSE de
    l'interface ; le fichier de travail couvre les téléchargements automatiques.
    """

    def __init__(self, total: int):
        self.total = max(0, int(total))
        self.fait = 0
        self._publier()
        if self.total:
            # État déterminé avant le premier transfert. INNER, dans serve.py,
            # interprète cette ligne comme exactement 0/total.
            print(f"  [1/{self.total}] 0%")

    def _publier(self) -> None:
        runtime.travail(
            "Téléchargement des clips", self.fait, self.total,
            cle="phase.download_clips",
        )

    def commencer(self, nom: str) -> None:
        if self.total:
            position = min(self.fait + 1, self.total)
            print(f"  [{position}/{self.total}] {nom}")
        self._publier()

    def terminer(self) -> None:
        if self.fait < self.total:
            self.fait += 1
        self._publier()
        if self.total:
            # Publication explicite après le résultat : auparavant le dernier
            # état restait à N-1/N, puis le fichier disparaissait aussitôt.
            print(f"  [{self.fait}/{self.total}] 100%")

    def finir(self) -> None:
        """Garantit un dernier état N/N, même après une branche d'erreur."""
        self.fait = self.total
        self._publier()


class _PlanUSB:
    def __init__(self, nom, sync, clips, pending, adopted):
        self.nom = nom
        self.sync = sync
        self.clips = clips
        self.pending = pending
        self.adopted = adopted
        self.jobs = []


class _PlanCloud:
    def __init__(self, clips=None, pending=None, adopted=0, skipped=0):
        self.clips = clips or []
        self.pending = pending or []
        self.adopted = adopted
        self.skipped = skipped


class _DownloadJob:
    """Un clip logique, avec une source USB prioritaire et un repli cloud."""

    def __init__(self, usb=None, cloud=None):
        self.usb = usb
        self.cloud = cloud
        self.done = False


class _ResultatPassage(NamedTuple):
    code: int
    execute: bool


async def download_clip(blink: Blink, clip, target: Path, overwrite: bool) -> str:
    """Prépare puis télécharge un clip, sans jamais le supprimer du hub.

    La suppression éventuelle (issue GitHub #1) est décidée par l'appelant,
    après coup : voir un_passage() et runtime.lire_suppression_auto()."""
    # Un fichier non inscrit trouvé au chemin attendu doit subir la même
    # validation approfondie qu'un nouveau transfert avant d'être adopté.
    if target.exists() and md.valid_mp4_complet(target) and not overwrite:
        return "skipped"

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    partial.unlink(missing_ok=True)

    try:
        prepared = await clip.prepare_download(blink)
        if not prepared or not await clip.download_video(blink, str(partial)):
            return "failed"
        # La sonde parcourt les paquets du .part avant son renommage : un
        # ftyp/moov intact ne suffit pas si mdat a été écourté en transit.
        if not partial.exists() or not md.valid_mp4_complet(partial):
            return "failed"
        partial.replace(target)
        return "downloaded"
    finally:
        partial.unlink(missing_ok=True)


async def _inventorier_cloud(blink: Blink, args, output: Path,
                             state: dict) -> _PlanCloud:
    """Construit le plan cloud sans commencer le moindre transfert."""
    clips = await blink_models.read_cloud_manifest(blink, args.since)
    if args.camera:
        clips = [c for c in clips if c.name.casefold() == args.camera.casefold()]
    if not clips:
        return _PlanCloud()

    print("\n--- Inventaire du cloud de l'abonnement ---")
    # Le rapprochement se fait avec ce qui est déjà au registre, et non avec le
    # manifeste USB : celui-ci ne montre que ce que la clé contient encore,
    # alors que le registre garde la trace de tout ce qui a été rapatrié.
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
        return _PlanCloud(
            clips=clips,
            skipped=len(doublons) + ignores_exclus,
        )

    pending = []
    adopted = 0
    for clip in sorted(inedits, key=blink_models.clip_datetime_utc):
        sync = _HubCloud(clip.network_id)
        target = blink_models.target_path(output, clip, sync=sync, source="cloud")
        _, entree_connue = blink_registre._trouver_entree(
            state, sync, clip, source="cloud",
        )
        if (
            entree_connue is None
            and target.exists()
            and target.is_file()
            and md.valid_mp4_complet(target)
            and not args.overwrite
        ):
            blink_registre.remember_download(state, sync, args.hub or "cloud", clip, output,
                              target, source="cloud")
            blink_registre.save_download_state(output, state)
            adopted += 1
            continue
        pending.append(clip)

    return _PlanCloud(
        clips=clips,
        pending=pending,
        adopted=adopted,
        skipped=len(doublons) + ignores_exclus,
    )


async def _telecharger_cloud(blink: Blink, args, output: Path, state: dict,
                             plan: _PlanCloud, jobs: list,
                             progression: _ProgressionTelechargement) -> CloudResult:
    """Exécute les jobs cloud seuls ou les replis d'un job USB en échec."""
    if not plan.clips:
        return CloudResult()

    suppression_auto = runtime.lire_suppression_auto()
    downloaded = failed = adopted_execution = skipped_execution = 0
    # Un job déjà terminé a réussi par l'USB : son équivalent cloud est bien
    # un doublon, jamais un second clip à faire avancer dans la barre.
    skipped_usb = sum(1 for job in jobs if job.cloud is not None and job.done)
    jobs_a_executer = [
        job for job in jobs if job.cloud is not None and not job.done
    ]
    # Un titre de phase remet la barre SSE en mode indéterminé. Ne l'émettre
    # que si une vraie opération cloud va donc produire les ticks suivants ;
    # lorsque tous ses clips sont déjà venus de l'USB, la barre doit rester à
    # N/N au lieu de finir visuellement sur un spinner.
    if jobs_a_executer:
        print("\n=== CLOUD DE L'ABONNEMENT ===")

    for job in jobs_a_executer:
        clip = job.cloud
        sync = _HubCloud(clip.network_id)
        target = blink_models.target_path(output, clip, sync=sync, source="cloud")
        progression.commencer(target.name)
        resultat = "failed"
        partiel = target.with_suffix(target.suffix + ".part")
        partiel.unlink(missing_ok=True)
        try:
            # Revalidation au dernier moment : un autre chemin du même passage
            # peut avoir acquis le média depuis la phase d'inventaire.
            _, entree_connue = blink_registre._trouver_entree(
                state, sync, clip, source="cloud",
            )
            if (
                isinstance(entree_connue, dict)
                and blink_registre._entree_acquise(output, entree_connue)
                and not args.overwrite
            ):
                resultat = "skipped"
            elif (
                entree_connue is None
                and target.exists()
                and target.is_file()
                and md.valid_mp4_complet(target)
                and not args.overwrite
            ):
                blink_registre.remember_download(
                    state, sync, args.hub or "cloud", clip, output, target,
                    source="cloud",
                )
                blink_registre.save_download_state(output, state)
                adopted_execution += 1
                resultat = "adopted"
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                telecharge = await clip.download_to(blink, partiel)
                if telecharge and md.valid_mp4_complet(partiel):
                    partiel.replace(target)
                    blink_registre.remember_download(
                        state, sync, args.hub or "cloud", clip, output, target,
                        source="cloud",
                    )
                    blink_registre.save_download_state(output, state)
                    downloaded += 1
                    resultat = "downloaded"
                    if blink_registre.camera_setting_key(sync, clip) in suppression_auto:
                        if await clip.delete_video(blink):
                            print("    Supprimé du cloud (caméra en suppression "
                                  "automatique).")
                            state["clips"][
                                blink_registre.state_key(sync, clip, source="cloud")
                            ]["source_deleted"] = True
                            blink_registre.save_download_state(output, state)
                        else:
                            print("    ! Échec de la suppression sur le cloud "
                                  "(clip conservé là-bas).")
                else:
                    if telecharge:
                        clip.download_issue = (
                            "données", "MP4 tronqué ou illisible après téléchargement",
                        )
                    categorie, detail = getattr(
                        clip, "download_issue", None
                    ) or ("média", "contenu indisponible")
                    print(f"    Échec [{categorie}] : {detail}.")
        except (ClientError, OSError, asyncio.TimeoutError) as erreur:
            print(f"    Échec [réseau] : {type(erreur).__name__}.")
        except Exception as erreur:  # Isoler un clip invalide des suivants.
            print(f"    Échec [données] : {type(erreur).__name__}.")
        finally:
            partiel.unlink(missing_ok=True)

        if resultat == "failed":
            failed += 1
        elif resultat == "skipped":
            skipped_execution += 1
        job.done = True
        progression.terminer()

    resultat = CloudResult(
        downloaded=downloaded,
        adopted=plan.adopted + adopted_execution,
        skipped=plan.skipped + skipped_usb + skipped_execution,
        failed=failed,
    )
    print(
        f"  Terminé : {resultat.downloaded} téléchargé(s), "
        f"{resultat.adopted} adopté(s), {resultat.skipped} ignoré(s), "
        f"{resultat.failed} échec(s)."
    )
    return resultat


async def traiter_cloud(blink: Blink, args, modules: list) -> CloudResult:
    """Inventorie puis télécharge le cloud avec un compteur complet 0/N–N/N.

    ``un_passage`` utilise les mêmes briques avec les jobs USB pour former un
    total global. Cette enveloppe reste disponible aux tests et aux appelants
    cloud-only historiques.
    """
    output = args.output.resolve()
    state = blink_registre.load_download_state(output)
    if args.command == "download":
        runtime.travail(
            "Inventaire des clips", 0, 0, cle="phase.inventory_clips",
        )
    plan = await _inventorier_cloud(blink, args, output, state)
    if args.command != "download":
        return CloudResult(skipped=plan.skipped)
    jobs = [_DownloadJob(cloud=clip) for clip in plan.pending]
    progression = _ProgressionTelechargement(len(jobs))
    resultat = await _telecharger_cloud(
        blink, args, output, state, plan, jobs, progression,
    )
    progression.finir()
    return resultat


async def _faire_passage(blink: Blink, args, modules: list,
                         repetition: bool) -> _ResultatPassage:
    """Exécute un passage sous son verrou et nettoie toujours sa publication."""
    execute = False
    try:
        # Un seul rapatriement à la fois, quelle que soit la source : deux
        # processus qui prennent le même clip écriraient le même fichier
        # partiel, et le premier renommage laisserait l'autre dans le vide.
        with runtime.verrou("download", "download", attente=30):
            execute = True
            try:
                code = await un_passage(blink, args, modules)
            finally:
                # Le dernier N/N reste brièvement consultable par la sonde web,
                # sans être considéré comme un travail encore actif.
                runtime.fin_travail(conserver=10)
            return _ResultatPassage(code, True)
    except runtime.BusyError as erreur:
        print(f"Téléchargement déjà en cours ({erreur}).")
        # Dans une boucle, l'autre worker fait déjà le travail et le prochain
        # tour réessaiera. Pour un clic manuel, répondre succès ferait lancer
        # la fusion puis afficher « terminé » alors qu'aucun téléchargement de
        # ce clic n'a eu lieu.
        return _ResultatPassage(0 if repetition else 1, False)
    except Exception as erreur:
        print(f"Tour de téléchargement interrompu par une erreur, "
              f"on réessaie au prochain : {erreur}")
        return _ResultatPassage(1, execute)


async def _attendre_echeance(echeance: float) -> bool:
    """Attend une échéance en restant sensible à « stop » chaque seconde."""
    while not runtime.arret_demande():
        reste = echeance - time.monotonic()
        if reste <= 0:
            return True
        await asyncio.sleep(min(1.0, reste))
    return False


async def _boucler_sources(blink: Blink, args, modules: list,
                           usb_minutes: int, cloud_minutes: int) -> int:
    """Planifie USB et cloud ensemble, avec deux cadences indépendantes.

    Le premier tour porte toujours sur ``all`` : après un changement de
    stockage, l'inventaire connaît donc tous les clips avant le premier octet
    transféré et publie un dénominateur unique. Les tours suivants ne réveillent
    chaque source qu'à sa propre cadence ; si les deux échéances coïncident, un
    nouveau passage global conserve la même propriété.
    """
    periode_usb = usb_minutes * 60
    periode_cloud = cloud_minutes * 60
    # Tant que le verrou n'a pas réellement été obtenu, le bootstrap global
    # n'est pas accompli. Une actualisation manuelle présente au démarrage ne
    # doit pas transformer le prochain vrai tour en cloud-only.
    while not runtime.arret_demande():
        debut = time.monotonic()
        premier = copy.copy(args)
        premier.source = "all"
        resultat = await _faire_passage(blink, premier, modules, repetition=True)
        if resultat.execute:
            break
        if not await _attendre_echeance(time.monotonic() + 1):
            return 0
    else:
        return 0
    echeance_usb = debut + periode_usb
    echeance_cloud = debut + periode_cloud
    if not modules:
        echeance_usb = float("inf")

    while not runtime.arret_demande():
        prochaine = min(echeance_usb, echeance_cloud)
        if not await _attendre_echeance(prochaine):
            break

        maintenant = time.monotonic()
        usb_du = maintenant >= echeance_usb
        cloud_du = maintenant >= echeance_cloud
        tour = copy.copy(args)
        tour.source = "all" if usb_du and cloud_du else ("usb" if usb_du else "cloud")
        await _faire_passage(blink, tour, modules, repetition=True)

        # Échéances ancrées au début, jamais à la fin du passage : un tour long
        # saute ses créneaux dépassés sans décaler définitivement les suivants.
        maintenant = time.monotonic()
        if usb_du:
            while echeance_usb <= maintenant:
                echeance_usb += periode_usb
        if cloud_du:
            while echeance_cloud <= maintenant:
                echeance_cloud += periode_cloud
    return 0


async def boucler(blink: Blink, args, modules: list) -> int:
    """Répète les passages demandés en gardant la session Blink ouverte.

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
    usb_minutes = getattr(args, "usb_loop", None)
    cloud_minutes = getattr(args, "cloud_loop", None)
    if usb_minutes is not None and cloud_minutes is not None:
        return await _boucler_sources(
            blink, args, modules, usb_minutes, cloud_minutes,
        )

    while not runtime.arret_demande():
        echeance = time.monotonic() + (args.loop or 0) * 60
        resultat = await _faire_passage(
            blink, args, modules, repetition=bool(args.loop),
        )
        code = resultat.code
        if not args.loop:
            return code
        # Échéance calculée depuis le début du tour, pas depuis sa fin : un
        # tour plus lent que la cadence ne doit pas décaler tous les suivants
        # d'autant (O-05). Un tour trop lent saute simplement son repos.
        if not await _attendre_echeance(echeance):
            break
    return 0


async def un_passage(blink: Blink, args, modules: list) -> int:
    """Un tour : inventorie tout, puis exécute un plan global et dédupliqué."""
    had_error = False
    neufs_total = 0

    # ``list`` n'a rien à transférer : conserver son chemin léger et ses
    # sorties par source, sans créer un faux travail dans l'interface.
    if args.command != "download":
        for name, sync in ([] if args.source == "cloud" else modules):
            print(f"\n=== STOCKAGE LOCAL : {name} ===")
            try:
                with hub_lock(name):
                    clips = await blink_models.read_local_manifest(sync)
            except runtime.BusyError as error:
                print(f"  Module occupé (direct ou actualisation en cours) : {error}.")
                had_error = True
                continue
            except RuntimeError as error:
                print(f"  Indisponible : {error}.")
                had_error = True
                continue
            clips = blink_models.filter_clips(clips, args.camera, args.since)
            blink_models.print_clip_summary(clips)
        if args.source != "usb":
            resultat_cloud = await traiter_cloud(blink, args, modules)
            had_error = resultat_cloud.had_error or had_error
        return 1 if had_error else 0

    output = args.output.resolve()
    state = blink_registre.load_download_state(output)
    runtime.travail("Inventaire des clips", 0, 0,
                    cle="phase.inventory_clips")

    # Phase 1 : tous les manifestes et tous les filtres avant le premier octet
    # transféré. C'est la seule manière de connaître un vrai total global.
    plans_usb = []
    for name, sync in ([] if args.source == "cloud" else modules):
        print(f"\n--- Inventaire du stockage local : {name} ---")
        runtime.travail("Inventaire des clips", 0, 0,
                        cle="phase.inventory_clips")
        try:
            with hub_lock(name):
                clips = await blink_models.read_local_manifest(sync)
        except runtime.BusyError as error:
            print(f"  Module occupé (direct ou actualisation en cours) : {error}.")
            had_error = True
            continue
        except RuntimeError as error:
            print(f"  Indisponible : {error}.")
            had_error = True
            continue

        clips = blink_models.filter_clips(clips, args.camera, args.since)
        blink_models.print_clip_summary(clips)
        print(f"  Destination : {output}")
        pending = []
        adopted = 0
        index_registre = blink_registre._index_registre(state)
        correspondances = blink_registre._apparier_registre(
            state, sync, clips, index_registre,
        )
        for indice_clip, clip in enumerate(clips):
            target = blink_models.target_path(
                output, clip, sync=sync, source="usb",
            )
            _, entree_connue = correspondances.get(indice_clip, (None, None))
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
                and md.valid_mp4_complet(target)
                and not args.overwrite
            ):
                blink_registre.remember_download(
                    state, sync, name, clip, output, target,
                )
                adopted += 1
                continue
            pending.append(clip)

        blink_registre.save_download_state(output, state)
        deja = len(clips) - len(pending)
        print(f"  Incrémental : {len(pending)} nouveau(x), {deja} déjà acquis.")
        plans_usb.append(_PlanUSB(name, sync, clips, pending, adopted))

    plan_cloud = _PlanCloud()
    if args.source != "usb":
        runtime.travail("Inventaire des clips", 0, 0,
                        cle="phase.inventory_clips")
        try:
            plan_cloud = await _inventorier_cloud(blink, args, output, state)
        except Exception as error:
            # Un cloud momentanément indisponible ne doit pas jeter le plan USB
            # déjà inventorié : il sera retenté au prochain passage.
            print(f"  Cloud indisponible : {type(error).__name__}: {error}")
            had_error = True

    # Un job par clip USB ; un clip cloud corrélé devient son repli. Les clips
    # cloud sans pendant USB forment leurs propres jobs. ``--overwrite`` garde
    # volontairement les deux téléchargements, comme auparavant.
    jobs = []
    clips_usb = []
    sync_par_clip = {}
    for plan in plans_usb:
        for clip in plan.pending:
            job = _DownloadJob(usb=(plan, clip))
            plan.jobs.append(job)
            jobs.append(job)
            clips_usb.append(clip)
            sync_par_clip[id(clip)] = plan.sync

    cloud_apparies = set()
    if clips_usb and plan_cloud.pending and not args.overwrite:
        paires = blink_models._apparier_evenements(
            clips_usb,
            plan_cloud.pending,
            compatibles=lambda local, distant: blink_models._meme_camera(
                local, distant, sync_gauche=sync_par_clip.get(id(local)),
            ),
        )
        for indice_usb, indice_cloud in paires:
            jobs[indice_usb].cloud = plan_cloud.pending[indice_cloud]
            cloud_apparies.add(indice_cloud)
    for indice, clip in enumerate(plan_cloud.pending):
        if indice not in cloud_apparies:
            jobs.append(_DownloadJob(cloud=clip))

    progression = _ProgressionTelechargement(len(jobs))
    suppression_auto = runtime.lire_suppression_auto()

    # Phase 2a : USB, regroupé par module pour n'occuper qu'une source à la
    # fois. Un échec ayant un équivalent cloud garde le même job inachevé : le
    # repli le terminera sans modifier le dénominateur.
    for plan in plans_usb:
        if not plan.jobs:
            continue
        print(f"\n=== STOCKAGE LOCAL : {plan.nom} ===")
        downloaded = skipped = failed = echecs_sans_repli = 0
        try:
            with hub_lock(plan.nom):
                for job in plan.jobs:
                    clip = job.usb[1]
                    target = blink_models.target_path(
                        output, clip, sync=plan.sync, source="usb",
                    )
                    progression.commencer(target.name)
                    try:
                        resultat = await download_clip(
                            blink, clip, target, args.overwrite,
                        )
                    except Exception as error:
                        print(f"    Échec : {type(error).__name__}: {error}")
                        resultat = "failed"

                    if resultat == "downloaded":
                        downloaded += 1
                        blink_registre.remember_download(
                            state, plan.sync, plan.nom, clip, output, target,
                        )
                        blink_registre.save_download_state(output, state)
                        if blink_registre.camera_setting_key(plan.sync, clip) in suppression_auto:
                            # La copie locale est déjà valide et inscrite. Une
                            # panne de l'API de suppression ne doit ni annuler
                            # ce succès, ni interrompre tous les clips suivants,
                            # ni laisser la barre avant N/N.
                            try:
                                supprime = await clip.delete_video(blink)
                            except Exception as error:
                                print("    ! Suppression impossible sur le Sync "
                                      f"Module ({type(error).__name__}) ; clip "
                                      "conservé là-bas.")
                            else:
                                if supprime:
                                    print("    Supprimé du Sync Module (caméra en "
                                          "suppression automatique).")
                                    state["clips"][
                                        blink_registre.state_key(plan.sync, clip)
                                    ]["source_deleted"] = True
                                    blink_registre.save_download_state(output, state)
                                else:
                                    print("    ! Échec de la suppression sur le "
                                          "Sync Module (clip conservé là-bas).")
                    elif resultat == "skipped":
                        skipped += 1
                        if target.exists() and md.valid_mp4(target):
                            blink_registre.remember_download(
                                state, plan.sync, plan.nom, clip, output, target,
                            )
                            blink_registre.save_download_state(output, state)
                    else:
                        failed += 1
                        if job.cloud is None:
                            echecs_sans_repli += 1
                        print("    Échec du téléchargement après plusieurs tentatives.")

                    if resultat != "failed" or job.cloud is None:
                        job.done = True
                        progression.terminer()
        except runtime.BusyError as error:
            print(f"  Module occupé (direct ou actualisation en cours) : {error}.")
            failed = len(plan.jobs)
            for job in plan.jobs:
                if job.done:
                    continue
                clip = job.usb[1]
                target = blink_models.target_path(
                    output, clip, sync=plan.sync, source="usb",
                )
                progression.commencer(target.name)
                if job.cloud is None:
                    echecs_sans_repli += 1
                    job.done = True
                    progression.terminer()

        print(
            f"  Terminé : {downloaded} téléchargé(s), "
            f"{skipped} déjà présent(s), {failed} échec(s)."
        )
        # Un échec USB corrélé au cloud n'est pas encore définitif : le même job
        # sera tenté juste après par cette seconde source. Seul l'échec du repli
        # (compté par resultat_cloud) doit alors rendre le passage non nul.
        had_error = had_error or echecs_sans_repli > 0
        neufs_total += downloaded

    # Phase 2b : cloud seul ou repli d'un USB en échec.
    if args.source != "usb":
        resultat_cloud = await _telecharger_cloud(
            blink, args, output, state, plan_cloud, jobs, progression,
        )
        had_error = resultat_cloud.had_error or had_error
        neufs_total += resultat_cloud.new_downloads

    progression.finir()

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
            # Le port configuré, pas 8765 en dur : sans ça, la notification
            # pointait vers la mauvaise page dès que l'utilisateur changeait
            # de port dans les réglages (revue du 27/08, bug 5).
            port = runtime.lire_reglages()["port"]
            runtime.toast("Blink", corps, url=f"http://127.0.0.1:{port}/")

    return 1 if had_error else 0
