"""Diagnostic WebRTC local, sans caméra, identifiants ni service STUN.

À lancer isolément (pas pendant un direct) : les candidats ICE sont limités
à la boucle locale et les capacités H.264 temporaires sont ensuite restaurées.
La fixture est encodée par ffmpeg, mais la passerelle réelle reste en copie :
MPEG-TS -> _PisteH264 -> av.Packet -> RTP/DTLS/SRTP -> décodage PyAV.
"""

import asyncio
from contextlib import suppress
import platform

import runtime


async def verifier_webrtc(ffmpeg: str, timeout: float = 30.0,
                         profil_recepteur: str = "high", payload_type: int = 112) -> dict:
    """Décode trois images H.264 High locales et vérifie leur fermeture.

Lève une exception si une dépendance native, la négociation, le transfert ou
le nettoyage échoue. Le budget inclut cinq secondes réservées au nettoyage.
Les seuls sockets créés sont TCP/UDP sur 127.0.0.1 ; aucun compte Blink n'est
chargé. Compatible avec Python 3.8 / aiortc 1.9 / PyAV 12 et versions récentes.
"""
    if timeout <= 5:
        raise ValueError("Le budget WebRTC doit dépasser cinq secondes")
    if profil_recepteur not in ("high", "high444"):
        raise ValueError("Profil récepteur inconnu")

    import aioice.ice
    import aiortc
    import av
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from aiortc.rtcrtpparameters import RTCRtpCodecCapability
    from aiortc.sdp import SessionDescription

    import blink_ts_demux
    import blink_webrtc

    if not blink_webrtc.DISPONIBLE:
        raise RuntimeError("aiortc est installé mais la passerelle est indisponible")

    boucle = asyncio.get_running_loop()
    debut = boucle.time()
    candidats_origine = aioice.ice.get_host_addresses
    codecs_origine = list(blink_webrtc.CODECS["video"])
    profils_origine = set(blink_webrtc._PROFILS_ENREGISTRES)
    client = None
    serveur_pc = None
    serveur_tcp = None
    processus = None
    pistes = []
    taches_tcp = set()
    connexions_tcp = set()
    messages = []
    fermetures = 0
    tcp_ferme = asyncio.Event()
    resultat = {}

    async def on_close():
        nonlocal fermetures
        fermetures += 1

    async def echanger():
        nonlocal client, serveur_pc, serveur_tcp, processus
        # AUD et SPS/PPS dans le TS : exactement le format lu par la passerelle.
        processus = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "testsrc2=size=160x96:rate=10",
            "-t", "2", "-an", "-c:v", "libx264", "-preset", "veryfast",
            "-profile:v", "high", "-level:v", "3.1", "-pix_fmt", "yuv420p",
            "-x264-params", "aud=1:keyint=10:min-keyint=10:scenecut=0:bframes=0",
            "-f", "mpegts", "pipe:1",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, creationflags=runtime.SANS_FENETRE,
        )
        donnees, erreurs = await processus.communicate()
        if processus.returncode:
            raise RuntimeError("ffmpeg : " + erreurs.decode("utf-8", "replace"))
        parametres = b"".join(
            nal for _pts, nal in blink_ts_demux.DemuxeurTSVideo().alimenter(donnees)
            if blink_ts_demux.type_nal(nal) in (7, 8)
        )
        profil = blink_webrtc._profile_level_id(parametres)
        if not profil or not profil.startswith("64"):
            raise RuntimeError("La fixture ffmpeg n'est pas en H.264 High")

        async def servir(reader, writer):
            tache = asyncio.current_task()
            taches_tcp.add(tache)
            connexions_tcp.add(writer)
            try:
                writer.write(donnees)
                await writer.drain()
                # Ne pas simuler EOF avant lecture : sa fermeture doit être
                # initiée et attendue par le nettoyage de la vraie passerelle.
                await reader.read()
                tcp_ferme.set()
            finally:
                writer.close()
                with suppress(ConnectionError, asyncio.CancelledError):
                    await writer.wait_closed()
                connexions_tcp.discard(writer)
                taches_tcp.discard(tache)

        serveur_tcp = await asyncio.start_server(servir, "127.0.0.1", 0)
        port = serveur_tcp.sockets[0].getsockname()[1]
        # Le navigateur annonce High ; aiortc n'annonce que Baseline par
        # défaut. Même enregistrement de profil que dans la passerelle.
        profil_client = profil if profil_recepteur == "high" else "f400" + profil[4:]
        blink_webrtc._enregistrer_profil_h264(profil_client)
        # Simuler également les PT dynamiques bas de Chromium/Supermium.
        from copy import deepcopy
        blink_webrtc.CODECS["video"][:] = deepcopy(blink_webrtc.CODECS["video"])
        for codec in blink_webrtc.CODECS["video"]:
            if (codec.mimeType.lower() == "video/h264"
                    and codec.parameters.get("profile-level-id") == profil_client):
                codec.payloadType = payload_type
        client = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        transceiver = client.addTransceiver("video", direction="recvonly")
        transceiver.setCodecPreferences([
            RTCRtpCodecCapability(
                mimeType="video/H264", clockRate=90000,
                parameters={"level-asymmetry-allowed": "1",
                            "packetization-mode": "1", "profile-level-id": profil_client},
            ),
        ])
        reception = boucle.create_future()

        @client.on("track")
        def piste_recue(track):
            if not reception.done():
                reception.set_result(track)

        await client.setLocalDescription(await client.createOffer())
        # Les deux peers partagent le module aiortc dans ce diagnostic.
        # Rendre leurs PT différents révèle une réponse qui oublierait
        # d'adopter le PT bas de l'offre, comme dans le vrai navigateur.
        for codec in blink_webrtc.CODECS["video"]:
            if (codec.mimeType.lower() == "video/h264"
                    and codec.parameters.get("profile-level-id") == profil_client):
                codec.payloadType = 112
        serveur_pc, sdp, type_sdp = await blink_webrtc.negocier(
            "tcp://127.0.0.1:" + str(port), client.localDescription.sdp,
            client.localDescription.type, on_close, journal=messages.append,
        )
        for sender in serveur_pc.getSenders():
            if isinstance(sender.track, blink_webrtc._PisteH264):
                pistes.append(sender.track)
        if len(pistes) != 1:
            raise RuntimeError("La négociation n'utilise pas la piste H.264 réelle")
        if blink_webrtc._profile_level_id(pistes[0].sps_pps) != profil:
            raise RuntimeError("Le profil source a été modifié")
        codec_negocie = next(
            codec
            for media in SessionDescription.parse(sdp).media if media.kind == "video"
            for codec in media.rtp.codecs if codec.mimeType.lower() == "video/h264"
        )
        profil_negocie = codec_negocie.parameters["profile-level-id"]
        if profil_negocie != profil_client:
            raise RuntimeError("Le profil négocié ne correspond pas au récepteur")
        if codec_negocie.payloadType != payload_type:
            raise RuntimeError("Le payload type proposé par le récepteur n'est pas conservé")
        await client.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=type_sdp))
        track = await reception
        images = [await track.recv() for _ in range(3)]
        if any(not isinstance(frame, av.VideoFrame) for frame in images):
            raise RuntimeError("Aucune image H.264 réellement décodée")
        if any((frame.width, frame.height) != (160, 96) for frame in images):
            raise RuntimeError("Dimensions inattendues après décodage H.264")
        if not all(images[i].pts < images[i + 1].pts for i in range(len(images) - 1)):
            raise RuntimeError("Les images WebRTC n'avancent pas")
        rapports = await client.getStats()
        paquets = sum(
            getattr(rapport, "packetsReceived", 0) for rapport in rapports.values()
            if rapport.type == "inbound-rtp"
        )
        if paquets <= 0 or any(pc.connectionState != "connected"
                              for pc in (client, serveur_pc)):
            raise RuntimeError("Le transport ICE/DTLS/SRTP n'est pas connecté")
        resultat.update(
            python=platform.python_version(), aiortc=aiortc.__version__,
            av=av.__version__, codec="H264", profile_level_id=profil,
            negotiated_profile_level_id=profil_negocie,
            payload_type=codec_negocie.payloadType,
            frames_decoded=len(images), width=images[0].width, height=images[0].height,
            packets_received=paquets, loopback_only=True,
        )

    async def nettoyer():
        if serveur_pc is not None:
            await blink_webrtc.fermer_connexion(serveur_pc)
        if client is not None:
            await client.close()
        if processus is not None and processus.returncode is None:
            processus.kill()
            await processus.communicate()
        if serveur_tcp is not None:
            serveur_tcp.close()
            await serveur_tcp.wait_closed()
        for writer in tuple(connexions_tcp):
            writer.close()
        if taches_tcp:
            await asyncio.gather(*tuple(taches_tcp))

    # Seule substitution : l'énumération des adresses locales. Pas de faux
    # peer, codec, handshake, paquet, décodeur ni contexte TLS Blink.
    aioice.ice.get_host_addresses = lambda **_kwargs: ["127.0.0.1"]
    try:
        try:
            await asyncio.wait_for(echanger(), timeout=timeout - 5)
        finally:
            await asyncio.wait_for(nettoyer(), timeout=5)
    except Exception as error:
        raise RuntimeError(
            "Diagnostic WebRTC local : " + type(error).__name__ + ": "
            + str(error) + (" ; " + " ; ".join(messages) if messages else "")
        ) from error
    finally:
        aioice.ice.get_host_addresses = candidats_origine
        blink_webrtc.CODECS["video"][:] = codecs_origine
        blink_webrtc._PROFILS_ENREGISTRES.clear()
        blink_webrtc._PROFILS_ENREGISTRES.update(profils_origine)

    if (fermetures != 1 or not tcp_ferme.is_set()
            or any(pc.connectionState != "closed" for pc in (client, serveur_pc))
            or any(not piste._tache.done() for piste in pistes)):
        raise RuntimeError("Le nettoyage WebRTC/TCP/piste n'est pas terminé")
    resultat.update(closed=True, on_close_calls=fermetures,
                    elapsed_seconds=round(boucle.time() - debut, 3))
    return resultat
