"""Direct WebRTC : passerelle sans reencodage entre le flux Blink (immis,
via blinkpy) et un navigateur, en alternative a /live-mse (MSE + ffmpeg).

Mesure sur prototype le 2026-09-03 (cf. BACKLOG.md) : premiere image 2.3 a
2.9x plus rapide qu'en MSE, sans le cout CPU d'un reencodage - MSE doit
attendre le SPS pour ecrire un entete MP4 (empty_moov) avant d'emettre quoi
que ce soit, WebRTC negocie les parametres du codec a part (SDP), donc peut
demarrer des la premiere image-cle.

Optionnel par construction : DISPONIBLE reste False si aiortc n'est pas
installe (pip install -r requirements.txt pas encore fait, ou plateforme ou
la roue binaire n'existe pas). serve.py doit retomber sur MSE dans ce cas,
jamais faire echouer son propre import pour ce module.

Ecueils reels rencontres en construisant ceci, tous geres ici :
- PyAV (av.open()) extrait SPS/PPS dans codec_context.extradata au lieu de
  les laisser dans le flux de paquets demuxe, et sa sonde (analyzeduration/
  probesize) doit rester assez genereuse pour les capturer fiablement -
  baisser ce palier a l'aveugle a echoue en reel sur jardin (2026-09-03) :
  negociation SDP reussie, mais extradata incomplet, plus aucune image
  jamais decodee. D'ou blink_ts_demux.py : un demultiplexeur MPEG-TS/PES/NAL
  minimal mais correct (PAT -> PMT -> PID video -> flux elementaire), qui
  extrait SPS/PPS des les tout premiers octets, sans la marge de securite
  qu'il fallait garder pour la sonde de conteneur de PyAV.
- aiortc n'annonce en SDP que du H.264 Baseline (42001f/42e01f, code en dur
  dans aiortc/codecs/__init__.py) alors que les cameras Blink filment en
  High (souvent 640028) : le navigateur negocie Baseline, recoit du High,
  ignore tout en silence. Aucune solution officielle coté aiortc (issue
  aiortc/aiortc#944, fermee sans suite ; la reserve du mainteneur porte sur
  l'encodage, pas sur ce cas de passthrough). D'ou _enregistrer_profil_h264,
  qui ajoute le vrai profil (lu dans le SPS) a la table de capacites
  d'aiortc avant de repondre - un detail d'implementation non documente,
  susceptible de casser a une future mise a jour d'aiortc sans avertissement.
- RTCPeerConnection() sans configuration retombe sur un serveur STUN public
  par defaut (stun.l.google.com) : setLocalDescription() n'y renvoie
  qu'apres la collecte de candidats, un blocage reseau/pare-feu dessus a
  laisse MODULE_SLOT/le verrou disque tenus indefiniment en reel
  (2026-09-03). D'ou iceServers=[] (navigateur et serveur sur le meme
  reseau local, un STUN n'a de toute facon rien a apporter) et les deux
  plafonds durs (NEGOCIATION_MAX_SECONDS, SESSION_MAX_SECONDS) plus bas,
  memes principes que LIVE_FIRST_FRAME_SECONDS/LIVE_MAX_SECONDS en MSE."""

import asyncio
from contextlib import suppress
import time
from fractions import Fraction
from pathlib import Path
from typing import Awaitable, Callable, Optional

import runtime

try:
    import av
    from aiortc import (
        MediaStreamTrack,
        RTCConfiguration,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    from aiortc.codecs import CODECS
    from aiortc.mediastreams import MediaStreamError
    from aiortc.rtcrtpparameters import (
        RTCRtcpFeedback,
        RTCRtpCodecCapability,
        RTCRtpCodecParameters,
    )
    import blink_ts_demux
    DISPONIBLE = True
except ImportError:
    DISPONIBLE = False

# Mêmes plafonds durs qu'en MSE (LIVE_FIRST_FRAME_SECONDS/LIVE_MAX_SECONDS,
# serve.py), pour la même raison : sans borne, un blocage réseau ou une
# session jamais fermée proprement retient MODULE_SLOT/le verrou disque
# indéfiniment. NEGOCIATION_MAX_SECONDS reste large : offre/réponse SDP et
# poignée de main DTLS n'ont normalement rien à attendre de long.
NEGOCIATION_MAX_SECONDS = 15
# Distinct de NEGOCIATION_MAX_SECONDS depuis le 2026-09-03 (BACKLOG.md) :
# les deux partageaient jusque-la le meme plafond de 15s pour l'attente du
# SPS/PPS (sps_pps_pret), confondant deux choses sans rapport - la vitesse
# d'une negociation SDP/DTLS purement locale, et le temps que met une
# camera sur batterie a se reveiller. 15s suffit a la premiere, pas
# toujours a la seconde (timeout constate en reel sur jardin) : memes
# raison et valeur que LIVE_FIRST_FRAME_SECONDS cote MSE (serve.py), qui
# accorde deja cette patience pour la meme raison.
PREMIERE_IMAGE_MAX_SECONDS = 40
# Silence en cours de session (apres le premier SPS/PPS, donc une fois
# connectionState deja passe a "connected") : distinct de SESSION_MAX_SECONDS
# (300s), qui borne la duree totale d'un direct sain, pas le temps qu'on
# tolere sans le moindre octet. Sans ce plafond-la, _lire() (ci-dessous)
# reste bloque sur son `await reader.read()` tant que le relais Blink ne
# ferme pas proprement la connexion TCP - jamais garanti (WinError 10054
# "connexion fermee par l'hote distant" vu dans les logs asyncio APRES
# coup, pas au moment du blocage). Sans signal explicite, aiortc ne
# remarque rien de son cote (pas d'etat "disconnected", cf. plus haut) :
# connectionState restait a "connected" jusqu'au seul filet qui restait,
# SESSION_MAX_SECONDS - 5 minutes bloque sur "Reconnexion..." pour
# l'utilisateur (constate en reel, Salon, 2026-09-04). Meme ordre de
# grandeur qu'ATTENTE_HUB_MAX_SECONDS/le timeout de _stop_stream (serve.py,
# 20s chacun) plutot qu'une valeur inventee ici.
SILENCE_FLUX_MAX_SECONDS = 20
SESSION_MAX_SECONDS = 300
FERMETURE_MAX_SECONDS = 10

# Tampon de lecture (jitter buffer) avant d'emettre la toute premiere image,
# puis chaque image suivante cadencee sur son PTS a partir de cette meme
# ancre. Mesure en reel sur jardin (camera a pile, 2026-09-03, cf.
# BACKLOG.md) : un trou reseau regulier d'environ 1,0s (0,62 a 0,86s mesures
# sur 6 cycles), tres probablement le wifi de la camera qui s'endort par
# intervalles pour economiser la batterie - le PTS camera lui reste
# parfaitement regulier (30 fps sans saut), ce n'est donc pas un probleme
# d'encodage. Sans tampon, recv() renvoyait une image des sa
# demultiplexation : le moindre trou se voyait directement comme un arret de
# lecture (lecture saccadee, signale par l'utilisateur). Cout : un peu de la
# latence gagnee sur MSE, qui reste tres largement devant (jardin
# ~5,6s + 1,2s contre 15,8s en MSE). Applique uniformement (pas seulement
# aux cameras a pile) : plus simple, et Salon a largement la marge pour
# l'absorber sans redevenir plus lent que MSE.
TAMPON_LECTURE_SECONDS = 1.2
# Garde-fou mémoire si le navigateur cesse de consommer les images. Ne pas
# jeter arbitrairement des images H.264 : les suivantes peuvent en dépendre.
FILE_IMAGES_MAX = 900
FILE_IMAGES_MAX_OCTETS = 32 * 1024 * 1024
TAMPON_ENREGISTREMENT_MAX_OCTETS = 8 * 1024 * 1024


if DISPONIBLE:

    class _PisteH264(MediaStreamTrack):
        """Piste video sans decodage ni reencodage : lit le flux TCP local
        de blinkpy octet par octet, demultiplexe nous-memes (pas de PyAV,
        blink_ts_demux.py), regroupe les NAL units par unite d'acces
        (delimitee par un AUD, type 9 - c'est ainsi que la camera les
        emet), les fait passer telles quelles en av.Packet.

        recv() peut renvoyer un av.Packet plutot qu'un av.VideoFrame
        (documente dans la docstring de MediaStreamTrack.recv elle-meme) :
        RTCRtpSender (_next_encoded_frame, rtcrtpsender.py) detecte alors
        que ce n'est pas une Frame et appelle encoder.pack() au lieu de
        encoder.encode(), qui se contente de decouper le bitstream deja
        compresse en paquets RTP. Zero appel a un encodeur. Equivalent
        WebRTC du -c:v copy qu'utilise deja /live-mse.

        Horodatage : le vrai PTS encode par la camera (extrait de l'entete
        PES par blink_ts_demux, deja cadence a 90 kHz - meme horloge que la
        video RTP, aucune conversion a faire), pas l'heure d'arrivee. Un
        horodatage improvise a la reception a ete essaye puis retire
        (2026-09-03) : le reseau livre par rafales, pas a cadence reguliere,
        et le recepteur perd alors l'information de rythme dont il a besoin
        pour lisser l'affichage - lecture saccadee, constate en reel. Repli
        sur l'horloge locale seulement si jamais un PTS venait a manquer
        (ne devrait pas arriver : la camera en donne un par image).

        recv() cadence aussi sa sortie sur ce PTS, retenue derriere
        TAMPON_LECTURE_SECONDS (cf. plus haut) plutot que de renvoyer une
        image des qu'elle est demultiplexee : necessaire pour absorber le
        trou reseau periodique mesure sur les cameras a pile."""

        kind = "video"

        def __init__(
            self, reader: asyncio.StreamReader, ffmpeg: Optional[str] = None,
            chemin_enregistrement: Optional[Callable[[], Path]] = None,
            enregistrement_actif: Optional[Callable[[], bool]] = None,
            journal: Optional[Callable[[str], None]] = None,
        ) -> None:
            super().__init__()
            self._reader = reader
            self._demux = blink_ts_demux.DemuxeurTSVideo()
            self._t0 = time.monotonic()
            self._t_ancrage: Optional[float] = None
            self._pts_ancrage: Optional[int] = None
            self._unite_courante: list = []
            self._pts_courant = None
            self._file: asyncio.Queue = asyncio.Queue(maxsize=FILE_IMAGES_MAX)
            self._file_octets = 0
            self.sps_pps = b""
            self.sps_pps_pret = asyncio.Event()
            # Posés par negocier() après le SPS/PPS : toute fin du flux doit
            # fermer aussi la connexion, car track.stop() ne suffit pas à
            # faire transiter connectionState.
            self.pc = None
            self._demander_fermeture = None
            # Enregistrement disque, optionnel et pilote par bouton : voir
            # _synchroniser_enregistrement. chemin_enregistrement est une
            # fonction (pas un chemin deja resolu) : chaque demarrage doit
            # recalculer un horodatage frais, pas reprendre celui du debut
            # de session.
            self._ffmpeg = ffmpeg
            self._chemin_enregistrement = chemin_enregistrement
            self._enregistrement_actif = enregistrement_actif
            # No-op par defaut : negocier() sans camera identifiee (tests,
            # appelants futurs) ne doit jamais planter faute de journal.
            self._journal = journal or (lambda _message: None)
            self._recorder_process = None
            self._recorder_stdin = None
            self._recorder_demande = False
            self._recorder_demarrage = None
            self._recorder_attente = []
            self._recorder_attente_octets = 0
            self._tache = asyncio.ensure_future(self._lire())

        async def _lire(self) -> None:
            try:
                while True:
                    # None (pas de plafond) tant que le SPS/PPS initial n'est
                    # pas arrive : PREMIERE_IMAGE_MAX_SECONDS (40s, negocier())
                    # accorde deja cette patience-la, plus longue qu'ici a
                    # dessein (reveil d'une camera sur batterie) - un plafond
                    # plus court ici y couperait court en silence, avant meme
                    # que ce delai plus genereux n'ait sa chance.
                    delai = (
                        SILENCE_FLUX_MAX_SECONDS
                        if self.sps_pps_pret.is_set() else None
                    )
                    try:
                        morceau = await asyncio.wait_for(
                            self._reader.read(65536), timeout=delai
                        )
                    except asyncio.TimeoutError:
                        self._journal(
                            f"flux Blink silencieux depuis "
                            f"{SILENCE_FLUX_MAX_SECONDS}s, fermeture"
                        )
                        break
                    if not morceau:
                        break
                    for pts, nal in self._demux.alimenter(morceau):
                        type_ = blink_ts_demux.type_nal(nal)
                        if type_ == 9 and self._unite_courante:
                            unite = b"".join(self._unite_courante)
                            self._mettre_unite(self._pts_courant, unite)
                            self._synchroniser_enregistrement(unite)
                            self._unite_courante = []
                        if type_ == 9:
                            self._pts_courant = pts
                        self._unite_courante.append(nal)
                        if type_ in (7, 8) and not self.sps_pps_pret.is_set():
                            self.sps_pps += nal
                            if type_ == 8:  # PPS suit toujours SPS : les
                                self.sps_pps_pret.set()  # deux sont la
            except Exception as error:
                self._journal(
                    f"lecture du flux interrompue, "
                    f"{type(error).__name__}: {error}"
                )
            finally:
                # Toutes les fins du flux (EOF, reset, silence, annulation)
                # doivent fermer aussi WebRTC ; arrêter la piste seule ne
                # change pas connectionState et ne rend pas le module Blink.
                try:
                    self._arreter_enregistrement()
                finally:
                    self._terminer_file()
                    if self._demander_fermeture is not None:
                        self._demander_fermeture()

        def _mettre_unite(self, pts, unite: bytes) -> None:
            if (self._file.full()
                    or self._file_octets + len(unite) > FILE_IMAGES_MAX_OCTETS):
                raise RuntimeError("Le navigateur ne consomme plus le flux vidéo")
            self._file.put_nowait((pts, unite))
            self._file_octets += len(unite)

        def _terminer_file(self) -> None:
            # Libère les octets et réveille recv(), même si la lecture a été
            # annulée avant son tout premier tour de boucle.
            while not self._file.empty():
                self._file.get_nowait()
            self._file_octets = 0
            self._file.put_nowait(None)

        async def recv(self):
            item = await self._file.get()
            if item is None:
                self.stop()
                raise MediaStreamError
            pts, donnees = item
            self._file_octets -= len(donnees)
            if pts is None:
                pts = int((time.monotonic() - self._t0) * 90000)

            # Ancre posee sur la toute premiere image, puis chaque image
            # attend son tour selon l'ecart de PTS depuis cette ancre - pas
            # selon l'ordre d'arrivee. Un reseau qui rattrape un trou en
            # livrant plusieurs images d'un coup ne les fait donc pas
            # ressortir toutes en meme temps : elles restent espacees comme
            # a la prise de vue.
            if self._t_ancrage is None:
                self._t_ancrage = time.monotonic()
                self._pts_ancrage = pts
            cible = (
                self._t_ancrage + TAMPON_LECTURE_SECONDS
                + (pts - self._pts_ancrage) / 90000
            )
            attente = cible - time.monotonic()
            if attente > 0:
                await asyncio.sleep(attente)

            paquet = av.Packet(donnees)
            paquet.pts = pts
            paquet.time_base = Fraction(1, 90000)
            return paquet

        def _synchroniser_enregistrement(self, unite: bytes) -> None:
            """Démarre/arrête le sous-processus d'enregistrement selon l'état
            voulu par le bouton de la page (enregistrement_actif), puis lui
            transmet cette unité d'accès si un enregistrement est en cours.

            Consulté à chaque unité d'accès complète (plusieurs fois par
            seconde) : un clic sur le bouton met donc au plus une fraction
            de seconde à se voir ici, sans file d'attente ni événement
            dédié - même mécanique que le drapeau ENREGISTREMENT_DIRECT_
            ACTIF côté MSE (serve.py), lu à chaque bloc envoyé au navigateur."""
            if self._enregistrement_actif is None or self._chemin_enregistrement is None:
                return
            desire = self._enregistrement_actif()
            if (desire and not self._recorder_demande
                    and self.sps_pps_pret.is_set()
                    and (self._recorder_demarrage is None
                         or self._recorder_demarrage.done())):
                # Un MP4 commencé par une image dépendante n'est pas
                # décodable. Attendre le prochain IDR, sans réencoder.
                depart = 0
                while True:
                    depart = unite.find(b"\x00\x00\x01", depart)
                    if depart < 0:
                        return
                    depart += 3
                    if depart < len(unite) and unite[depart] & 0x1F == 5:
                        break
                self._recorder_demande = True
                self._recorder_attente = [unite]
                self._recorder_attente_octets = len(unite)
                self._recorder_demarrage = asyncio.create_task(
                    self._demarrer_enregistrement()
                )
                return
            elif not desire and self._recorder_demande:
                self._arreter_enregistrement()
                return  # deja arrete : rien a transmettre pour cette unite
            if self._recorder_demande and self._recorder_stdin is None:
                # create_subprocess_exec rend la main à la boucle : toutes
                # les unités reçues dans cet intervalle doivent suivre l'IDR.
                if (self._recorder_attente_octets + len(unite)
                        > TAMPON_ENREGISTREMENT_MAX_OCTETS):
                    self._journal("enregistrement arrêté : lancement trop lent")
                    self._arreter_enregistrement()
                    return
                self._recorder_attente.append(unite)
                self._recorder_attente_octets += len(unite)
                return
            self._ecrire_enregistrement(unite)

        async def _demarrer_enregistrement(self) -> None:
            """Lance un ffmpeg dedie qui remuxe (sans reencoder) le flux
            Annexe B deja demultiplexe ici vers un MP4 fragmente - meme
            format que /live-mse (frag_keyframe+empty_moov, serve.py), pour
            la meme raison : lisible meme coupe net (fermeture d'onglet,
            crash), sans dependre d'une sortie propre pour ecrire un moov
            final valide.

            Sous-processus separe de la RTCPeerConnection a dessein : si
            l'enregistrement ralentit (disque lent...), il ne doit jamais
            ralentir _lire() ni donc le direct affiche au navigateur.

            Les unités arrivées pendant le lancement sont conservées dans
            _recorder_attente, depuis un IDR et jusqu'à l'adoption du stdin.
            """
            try:
                chemin = self._chemin_enregistrement()
                chemin.parent.mkdir(parents=True, exist_ok=True)
                processus = await asyncio.create_subprocess_exec(
                    self._ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-f", "h264", "-i", "pipe:0",
                    "-c", "copy", "-f", "mp4",
                    "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                    str(chemin),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=runtime.SANS_FENETRE,
                )
            except Exception as error:
                # Permet un nouvel essai au prochain passage a "desire" :
                # sans ceci, un seul echec (ffmpeg introuvable...) bloquerait
                # tout enregistrement pour le reste de la session.
                self._journal(
                    f"echec de demarrage de l'enregistrement disque, "
                    f"{type(error).__name__}: {error}"
                )
                self._recorder_demande = False
                self._recorder_attente.clear()
                self._recorder_attente_octets = 0
                return
            if not self._recorder_demande or self.readyState == "ended":
                # Arrete (bouton, ou piste fermee) pendant que ce sous-
                # processus demarrait : personne n'en veut plus, ne pas
                # l'adopter - juste le laisser sortir proprement.
                try:
                    processus.stdin.close()
                except Exception:
                    pass
                asyncio.ensure_future(_attendre_fin_enregistrement(processus))
                return
            self._recorder_process = processus
            self._recorder_stdin = processus.stdin
            self._ecrire_enregistrement(self.sps_pps)
            attente = self._recorder_attente
            self._recorder_attente = []
            self._recorder_attente_octets = 0
            for unite in attente:
                self._ecrire_enregistrement(unite)

        def _ecrire_enregistrement(self, donnees: bytes) -> None:
            # write() ne bloque pas. Borner son tampon protège la mémoire
            # si ffmpeg/disque cesse de consommer, sans ralentir le direct.
            stdin = self._recorder_stdin
            if stdin is None:
                return
            try:
                if (stdin.transport.get_write_buffer_size() + len(donnees)
                        > TAMPON_ENREGISTREMENT_MAX_OCTETS):
                    raise BufferError("tampon disque plein")
                stdin.write(donnees)
            except Exception as error:
                # Une seule ligne meme si _lire() rappelle _ecrire_enregistrement
                # plusieurs fois par seconde : stdin passe a None des la
                # premiere erreur, donc ce bloc ne s'execute plus ensuite.
                self._journal(
                    f"enregistrement disque interrompu (ecriture), "
                    f"{type(error).__name__}: {error}"
                )
                self._arreter_enregistrement()

        def _arreter_enregistrement(self) -> None:
            self._recorder_demande = False
            self._recorder_attente.clear()
            self._recorder_attente_octets = 0
            stdin = self._recorder_stdin
            self._recorder_stdin = None
            if stdin is not None:
                try:
                    stdin.close()
                except Exception:
                    pass
            processus = self._recorder_process
            self._recorder_process = None
            if processus is not None:
                asyncio.ensure_future(_attendre_fin_enregistrement(processus))

        def fermer(self) -> None:
            self.stop()
            self._tache.cancel()
            self._terminer_file()
            try:
                self._arreter_enregistrement()
            except Exception as error:
                # Appelée depuis le nettoyage (negocier(), plus bas), avant
                # `await on_close()` : une exception non
                # rattrapée ici y interromprait la coroutine avant on_close,
                # qui libère MODULE_SLOT - direct.log en a montré un cas
                # réel (2026-09-04, "négocié" jamais suivi de "session
                # rendue normalement", verrou resté pris indéfiniment).
                # sous pythonw (autostart), sans console, une telle
                # exception ne serait de toute façon jamais vue nulle part
                # sans cette ligne : c'est exactement le cas que ce
                # journal doit désormais rendre visible.
                self._journal(
                    f"echec d'arret de l'enregistrement pendant la "
                    f"fermeture, {type(error).__name__}: {error}"
                )

    async def _attendre_fin_enregistrement(processus) -> None:
        """Laisse ffmpeg vider son tampon et sortir de lui-meme une fois
        stdin ferme (fermer(), ci-dessus) ; au-dela, meme plafond (5s) que
        le nettoyage ffmpeg de send_live_mse (serve.py)."""
        try:
            await asyncio.wait_for(processus.wait(), timeout=5)
        except Exception:
            try:
                processus.kill()
                await asyncio.wait_for(processus.wait(), timeout=5)
            except Exception:
                pass

    def _profile_level_id(sps_pps_annexb: bytes) -> Optional[str]:
        """profile_idc/level_idc (ex. "640028") a partir d'un SPS Annexe B.

        Ces 3 octets suivent toujours l'entete NAL en tete de SPS (ITU-T
        H.264, 7.3.2.1.1)."""
        i = sps_pps_annexb.find(b"\x00\x00\x01")
        if i == -1:
            return None
        i += 3
        if (sps_pps_annexb[i] & 0x1F) != 7:  # nal_unit_type 7 = SPS
            return None
        return sps_pps_annexb[i + 1 : i + 4].hex()

    _PROFILS_ENREGISTRES: set = set()

    def _enregistrer_profil_h264(profil: str) -> None:
        """Ajoute ce profile-level-id a aiortc.codecs.CODECS["video"].

        get_capabilities() (aiortc/codecs/__init__.py) lit directement
        cette liste, remplie une fois par init_codecs() avec seulement du
        Baseline. setCodecPreferences() valide son argument contre cette
        meme liste ("Codec is not in capabilities" sinon)."""
        if profil in _PROFILS_ENREGISTRES:
            return
        _PROFILS_ENREGISTRES.add(profil)
        pt = 110 + 2 * len(_PROFILS_ENREGISTRES)
        CODECS["video"] += [
            RTCRtpCodecParameters(
                mimeType="video/H264", clockRate=90000, payloadType=pt,
                rtcpFeedback=[
                    RTCRtcpFeedback(type="nack"),
                    RTCRtcpFeedback(type="nack", parameter="pli"),
                    RTCRtcpFeedback(type="goog-remb"),
                ],
                parameters={
                    "level-asymmetry-allowed": "1",
                    "packetization-mode": "1",
                    "profile-level-id": profil,
                },
            ),
            RTCRtpCodecParameters(
                mimeType="video/rtx", clockRate=90000, payloadType=pt + 1,
                parameters={"apt": pt},
            ),
        ]


async def fermer_connexion(pc) -> None:
    """Ferme WebRTC et attend la libération du flux Blink associé.

    aiortc émet ses événements de fermeture sans attendre leurs coroutines.
    pc.close() seul ne garantit donc pas que on_close() a terminé.
    """
    demander = getattr(pc, "_blink_demander_fermeture", None)
    if demander is None:
        await pc.close()
    else:
        await asyncio.shield(demander())


async def negocier(
    stream_url: str, offer_sdp: str, offer_type: str,
    on_close: Callable[[], Awaitable[None]],
    ffmpeg: Optional[str] = None,
    chemin_enregistrement: Optional[Callable[[], Path]] = None,
    enregistrement_actif: Optional[Callable[[], bool]] = None,
    journal: Optional[Callable[[str], None]] = None,
):
    """Ouvre le flux Blink (sans decodage), negocie WebRTC, renvoie
    (pc, sdp_reponse, type_reponse).

    `on_close` est attendu (await) quand la connexion se termine (etat
    "closed" ou "failed") : c'est le seul signal de fin disponible ici, il
    n'y a pas de requete HTTP a garder ouverte comme en MSE - fermer
    l'onglet ferme la RTCPeerConnection cote navigateur, ce qui remonte
    jusqu'ici via connectionstatechange. Appele au plus une fois.

    `chemin_enregistrement`/`enregistrement_actif`, avec `ffmpeg` : si
    fournis, une copie du direct est écrite à ce chemin tant que ce dernier
    répond vrai - piloté par le bouton de la page (voir
    _PisteH264._synchroniser_enregistrement)."""
    if not DISPONIBLE:
        raise RuntimeError(
            "aiortc n'est pas installe (pip install -r requirements.txt)"
        )
    journal = journal or (lambda _message: None)

    host, port_str = stream_url.replace("tcp://", "").split(":")
    writer = None
    track = None
    pc = None
    tache_plafond = None
    tache_fermeture = None

    async def _fermer_ressources() -> None:
        # Cette tâche unique est protégée de l'annulation de negocier() et
        # des demandes HTTP. Elle possède le nettoyage jusqu'à sa fin.
        if tache_plafond is not None:
            tache_plafond.cancel()
        try:
            if track is not None:
                try:
                    track.fermer()
                except Exception as error:
                    journal(f"fermeture piste : {type(error).__name__}: {error}")
                with suppress(asyncio.CancelledError, Exception):
                    await track._tache
            if writer is not None:
                try:
                    writer.close()
                    await asyncio.wait_for(writer.wait_closed(), timeout=5)
                except Exception as error:
                    journal(f"fermeture TCP : {type(error).__name__}: {error}")
            if pc is not None:
                try:
                    await asyncio.wait_for(pc.close(), timeout=FERMETURE_MAX_SECONDS)
                except Exception as error:
                    journal(f"fermeture WebRTC : {type(error).__name__}: {error}")
        finally:
            await on_close()

    def _demander_fermeture():
        nonlocal tache_fermeture
        if tache_fermeture is None:
            tache_fermeture = asyncio.create_task(_fermer_ressources())
            # Les fins spontanées (EOF/état failed) n'ont pas d'appelant
            # attendant cette tâche : consommer et journaliser leur erreur.
            def _fin(tache):
                if not tache.cancelled() and tache.exception() is not None:
                    error = tache.exception()
                    journal(f"nettoyage WebRTC : {type(error).__name__}: {error}")
            tache_fermeture.add_done_callback(_fin)
        return tache_fermeture

    try:
        reader, writer = await asyncio.open_connection(host, int(port_str))
        track = _PisteH264(
            reader, ffmpeg=ffmpeg, chemin_enregistrement=chemin_enregistrement,
            enregistrement_actif=enregistrement_actif, journal=journal,
        )
        # Le temps que met la camera a se reveiller et a livrer son
        # SPS/PPS - PREMIERE_IMAGE_MAX_SECONDS (40s, cf. plus haut), pas
        # NEGOCIATION_MAX_SECONDS (15s, pour la suite, purement locale).
        try:
            attente_sps = asyncio.create_task(track.sps_pps_pret.wait())
            try:
                terminees, _ = await asyncio.wait(
                    (attente_sps, track._tache),
                    timeout=PREMIERE_IMAGE_MAX_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not terminees:
                    raise asyncio.TimeoutError
                if track._tache.done():
                    raise RuntimeError("Le flux Blink s'est arrêté avant la première image.")
            finally:
                attente_sps.cancel()
                with suppress(asyncio.CancelledError):
                    await attente_sps
        except asyncio.TimeoutError:
            # Sans ceci, l'utilisateur ne voyait qu'un "TimeoutError: " brut
            # (constate en reel, 2026-09-03) - RuntimeError s'affiche tel
            # quel cote page (send_offer_webrtc, serve.py), meme traitement
            # que les messages MSE deja clairs pour ce cas.
            raise RuntimeError(
                "La camera n'a envoye aucune image. Hors de portee du "
                "module, endormie, ou deja occupee par une autre session."
            ) from None

        # iceServers=[] : sans ceci, aiortc retombe sur son defaut
        # (stun:stun.l.google.com:19302, RTCIceGatherer.getDefaultIceServers)
        # et setLocalDescription() n'y renvoie qu'une fois la collecte de
        # candidats terminee - un blocage reseau/pare-feu dessus bloque tout
        # indefiniment (constate en reel, cf. BACKLOG.md). Navigateur et
        # serveur sur le meme reseau local : aucun NAT a traverser, un STUN
        # n'a de toute facon rien a apporter ici.
        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
        track.pc = pc
        track._demander_fermeture = _demander_fermeture
        pc._blink_demander_fermeture = _demander_fermeture

        @pc.on("connectionstatechange")
        def _sur_changement_etat() -> None:
            journal(f"connectionstatechange -> {pc.connectionState}")
            if pc.connectionState in ("closed", "failed"):
                # Ne pas attendre pc.close() depuis son propre événement :
                # aiortc peut encore être en train de fermer les transports.
                _demander_fermeture()

        sender = pc.addTrack(track)
        profil = _profile_level_id(track.sps_pps)
        if profil:
            _enregistrer_profil_h264(profil)
            transceiver = next(
                t for t in pc.getTransceivers() if t.sender is sender
            )
            transceiver.setCodecPreferences([
                RTCRtpCodecCapability(
                    mimeType="video/H264", clockRate=90000,
                    parameters={
                        "level-asymmetry-allowed": "1",
                        "packetization-mode": "1",
                        "profile-level-id": profil,
                    },
                )
            ])

        # Filet de securite : meme sans STUN, rien ne garantit qu'une
        # negociation aboutisse toujours vite (reseau capricieux, bug
        # aiortc/aioice non identifie...). Sans plafond ici, un blocage
        # laisse MODULE_SLOT/le verrou disque tenus indefiniment - deja
        # observe en reel (cf. BACKLOG.md) : aucun connectionstatechange
        # n'arrive jamais si la negociation elle-meme ne se termine jamais.
        async def _echanger_sdp():
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=offer_sdp, type=offer_type)
            )
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

        await asyncio.wait_for(
            _echanger_sdp(), timeout=NEGOCIATION_MAX_SECONDS
        )
        if tache_fermeture is not None:
            raise RuntimeError("Le flux Blink s'est arrêté pendant la négociation.")
    except (Exception, asyncio.CancelledError):
        # CancelledError n'hérite pas d'Exception. L'expiration du budget
        # extérieur doit aussi fermer le TCP et rendre le module.
        with suppress(Exception):
            await asyncio.shield(_demander_fermeture())
        raise

    # Deuxieme filet, une fois la negociation reussie : une session qui ne
    # se ferme jamais proprement (onglet tue plutot que ferme, reseau qui
    # tombe sans notification...) ne doit pas non plus tenir la ressource
    # indefiniment - meme principe que LIVE_MAX_SECONDS cote MSE (serve.py).
    async def _plafond_duree() -> None:
        await asyncio.sleep(SESSION_MAX_SECONDS)
        if pc.connectionState not in ("closed", "failed"):
            journal(
                f"filet de {SESSION_MAX_SECONDS}s declenche, connectionState "
                f"restait a {pc.connectionState!r}"
            )
            await fermer_connexion(pc)

    tache_plafond = asyncio.create_task(_plafond_duree())

    return pc, pc.localDescription.sdp, pc.localDescription.type
