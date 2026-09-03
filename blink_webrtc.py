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
import time
from fractions import Fraction
from typing import Awaitable, Callable, Optional

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
# poignée de main DTLS n'ont normalement rien à attendre de long - mais
# borne aussi, maintenant, l'attente du SPS/PPS (cf. sps_pps_pret).
NEGOCIATION_MAX_SECONDS = 15
SESSION_MAX_SECONDS = 300

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

        def __init__(self, reader: asyncio.StreamReader) -> None:
            super().__init__()
            self._reader = reader
            self._demux = blink_ts_demux.DemuxeurTSVideo()
            self._t0 = time.monotonic()
            self._t_ancrage: Optional[float] = None
            self._pts_ancrage: Optional[int] = None
            self._unite_courante: list = []
            self._pts_courant = None
            self._file: asyncio.Queue = asyncio.Queue()
            self.sps_pps = b""
            self.sps_pps_pret = asyncio.Event()
            self._tache = asyncio.ensure_future(self._lire())

        async def _lire(self) -> None:
            try:
                while True:
                    morceau = await self._reader.read(65536)
                    if not morceau:
                        break
                    for pts, nal in self._demux.alimenter(morceau):
                        type_ = blink_ts_demux.type_nal(nal)
                        if type_ == 9 and self._unite_courante:
                            await self._file.put(
                                (self._pts_courant, b"".join(self._unite_courante))
                            )
                            self._unite_courante = []
                        if type_ == 9:
                            self._pts_courant = pts
                        self._unite_courante.append(nal)
                        if type_ in (7, 8) and not self.sps_pps_pret.is_set():
                            self.sps_pps += nal
                            if type_ == 8:  # PPS suit toujours SPS : les
                                self.sps_pps_pret.set()  # deux sont la
            except Exception:
                pass
            finally:
                if self._unite_courante:
                    await self._file.put(
                        (self._pts_courant, b"".join(self._unite_courante))
                    )
                await self._file.put(None)  # sentinelle de fin

        async def recv(self):
            item = await self._file.get()
            if item is None:
                self.stop()
                raise MediaStreamError
            pts, donnees = item
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

        def fermer(self) -> None:
            self._tache.cancel()

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


async def negocier(
    stream_url: str, offer_sdp: str, offer_type: str,
    on_close: Callable[[], Awaitable[None]],
):
    """Ouvre le flux Blink (sans decodage), negocie WebRTC, renvoie
    (pc, sdp_reponse, type_reponse).

    `on_close` est attendu (await) quand la connexion se termine (etat
    "closed" ou "failed") : c'est le seul signal de fin disponible ici, il
    n'y a pas de requete HTTP a garder ouverte comme en MSE - fermer
    l'onglet ferme la RTCPeerConnection cote navigateur, ce qui remonte
    jusqu'ici via connectionstatechange. Appele au plus une fois."""
    if not DISPONIBLE:
        raise RuntimeError(
            "aiortc n'est pas installe (pip install -r requirements.txt)"
        )

    host, port_str = stream_url.replace("tcp://", "").split(":")
    writer = None
    track = None
    pc = None
    try:
        reader, writer = await asyncio.open_connection(host, int(port_str))
        track = _PisteH264(reader)
        # Le seul temps d'attente reel de cette fonction : le temps que
        # SPS+PPS arrivent sur le flux. Meme plafond que le reste de la
        # negociation, meme raison (cf. plus bas).
        await asyncio.wait_for(
            track.sps_pps_pret.wait(), timeout=NEGOCIATION_MAX_SECONDS
        )

        # iceServers=[] : sans ceci, aiortc retombe sur son defaut
        # (stun:stun.l.google.com:19302, RTCIceGatherer.getDefaultIceServers)
        # et setLocalDescription() n'y renvoie qu'une fois la collecte de
        # candidats terminee - un blocage reseau/pare-feu dessus bloque tout
        # indefiniment (constate en reel, cf. BACKLOG.md). Navigateur et
        # serveur sur le meme reseau local : aucun NAT a traverser, un STUN
        # n'a de toute facon rien a apporter ici.
        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
        ferme = False

        @pc.on("connectionstatechange")
        async def _sur_changement_etat() -> None:
            nonlocal ferme
            if pc.connectionState in ("closed", "failed") and not ferme:
                ferme = True
                track.fermer()
                try:
                    writer.close()
                except Exception:
                    pass
                await on_close()

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
        await asyncio.wait_for(
            pc.setRemoteDescription(
                RTCSessionDescription(sdp=offer_sdp, type=offer_type)
            ),
            timeout=NEGOCIATION_MAX_SECONDS,
        )
        answer = await asyncio.wait_for(
            pc.createAnswer(), timeout=NEGOCIATION_MAX_SECONDS
        )
        await asyncio.wait_for(
            pc.setLocalDescription(answer), timeout=NEGOCIATION_MAX_SECONDS
        )
    except Exception:
        if track is not None:
            track.fermer()
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        if pc is not None:
            try:
                await pc.close()
            except Exception:
                pass
        raise

    # Deuxieme filet, une fois la negociation reussie : une session qui ne
    # se ferme jamais proprement (onglet tue plutot que ferme, reseau qui
    # tombe sans notification...) ne doit pas non plus tenir la ressource
    # indefiniment - meme principe que LIVE_MAX_SECONDS cote MSE (serve.py).
    async def _plafond_duree() -> None:
        await asyncio.sleep(SESSION_MAX_SECONDS)
        if pc.connectionState not in ("closed", "failed"):
            await pc.close()  # declenche connectionstatechange -> on_close

    asyncio.ensure_future(_plafond_duree())

    return pc, pc.localDescription.sdp, pc.localDescription.type
