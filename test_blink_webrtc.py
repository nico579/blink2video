"""Régressions WebRTC hors ligne : fins de flux, annulation et enregistreur."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import blink_webrtc


class FauxPeer:
    def __init__(self, **_kwargs):
        self.connectionState = "new"
        self.localDescription = SimpleNamespace(sdp="réponse", type="answer")
        self.callbacks = {}
        self.fermetures = 0
        self.track = None

    def on(self, evenement):
        def enregistrer(callback):
            self.callbacks[evenement] = callback
            return callback
        return enregistrer

    def addTrack(self, track):
        self.track = track

    async def setRemoteDescription(self, _description):
        pass

    async def createAnswer(self):
        return self.localDescription

    async def setLocalDescription(self, _description):
        self.connectionState = "connected"

    async def close(self):
        if self.connectionState == "closed":
            return
        self.fermetures += 1
        self.connectionState = "closed"
        self.callbacks["connectionstatechange"]()
        # L'événement est émis avant que close() ait rendu la main, comme
        # dans aiortc : le nettoyage ne doit pas attendre sa propre tâche.
        await asyncio.sleep(0)


@unittest.skipUnless(blink_webrtc.DISPONIBLE, "aiortc optionnel non installé")
class TestsCycleWebRTC(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.reader = asyncio.StreamReader()
        self.writer = SimpleNamespace(close=mock.Mock(), wait_closed=mock.AsyncMock())
        self.on_close = mock.AsyncMock()
        self.peers = []
        self.tracks = []

        def peer(**kwargs):
            resultat = FauxPeer(**kwargs)
            self.peers.append(resultat)
            return resultat

        piste_originale = blink_webrtc._PisteH264

        def piste(*args, **kwargs):
            resultat = piste_originale(*args, **kwargs)
            self.tracks.append(resultat)
            return resultat

        self.patches = [
            mock.patch.object(blink_webrtc.asyncio, "open_connection",
                              return_value=(self.reader, self.writer)),
            mock.patch.object(blink_webrtc, "RTCPeerConnection", side_effect=peer),
            mock.patch.object(blink_webrtc, "_PisteH264", side_effect=piste),
            mock.patch.object(blink_webrtc, "_profile_level_id", return_value=None),
            mock.patch.object(
                blink_webrtc.blink_ts_demux.DemuxeurTSVideo, "alimenter",
                return_value=[(0, b"\x00\x00\x01\x67\x64\x00\x28"),
                              (0, b"\x00\x00\x01\x68\x00")],
            ),
        ]
        for patch in self.patches:
            patch.start()

    async def asyncTearDown(self):
        for pc in self.peers:
            await blink_webrtc.fermer_connexion(pc)
        for track in self.tracks:
            track.fermer()
            await asyncio.gather(track._tache, return_exceptions=True)
        for patch in reversed(self.patches):
            patch.stop()

    async def negocier(self):
        return await blink_webrtc.negocier("tcp://127.0.0.1:1234", "offre", "offer",
                                          self.on_close)

    async def demarrer(self):
        self.reader.feed_data(b"SPS/PPS synthetiques")
        pc, _, _ = await self.negocier()
        return pc

    async def attendre_lecture(self):
        for _ in range(100):
            if self.tracks:
                return
            await asyncio.sleep(0)
        self.fail("La lecture du flux n'a pas démarré")

    async def test_eof_ferme_pc_tcp_et_annule_le_plafond(self):
        avant = asyncio.all_tasks()
        pc = await self.demarrer()
        plafond = [task for task in asyncio.all_tasks() - avant
                   if task.get_coro().__qualname__.endswith("._plafond_duree")]
        self.assertEqual(len(plafond), 1)

        self.reader.feed_eof()
        await asyncio.wait_for(pc.track._tache, 1)
        await asyncio.wait_for(blink_webrtc.fermer_connexion(pc), 1)

        self.assertEqual(pc.connectionState, "closed")
        self.assertEqual(pc.fermetures, 1)
        self.writer.close.assert_called_once()
        self.writer.wait_closed.assert_awaited_once()
        self.on_close.assert_awaited_once()
        self.assertTrue(plafond[0].done())

    async def test_reset_et_silence_ferment_aussi_la_session(self):
        pc = await self.demarrer()
        self.reader.set_exception(ConnectionResetError("relais interrompu"))
        await asyncio.wait_for(pc.track._tache, 1)
        await asyncio.wait_for(blink_webrtc.fermer_connexion(pc), 1)
        self.assertEqual(pc.connectionState, "closed")
        self.on_close.assert_awaited_once()

    async def test_silence_apres_sps_declenche_le_meme_nettoyage(self):
        with mock.patch.object(blink_webrtc, "SILENCE_FLUX_MAX_SECONDS", 0.01):
            pc = await self.demarrer()
            await asyncio.wait_for(pc.track._tache, 1)
            await asyncio.wait_for(blink_webrtc.fermer_connexion(pc), 1)
        self.assertEqual(pc.connectionState, "closed")
        self.on_close.assert_awaited_once()

    async def test_eof_avant_sps_echoue_sans_attendre_quarante_secondes(self):
        self.reader.feed_eof()
        with self.assertRaisesRegex(RuntimeError, "avant la première image"):
            await asyncio.wait_for(self.negocier(), 1)
        self.assertEqual(self.peers, [])
        self.writer.close.assert_called_once()
        self.on_close.assert_awaited_once()

    async def test_annulation_pendant_reveil_attend_le_nettoyage(self):
        nettoyage_demarre = asyncio.Event()
        finir_nettoyage = asyncio.Event()

        async def nettoyer():
            nettoyage_demarre.set()
            await finir_nettoyage.wait()

        self.on_close.side_effect = nettoyer
        negociation = asyncio.create_task(self.negocier())
        await self.attendre_lecture()
        negociation.cancel()
        await asyncio.wait_for(nettoyage_demarre.wait(), 1)
        self.assertFalse(negociation.done())
        self.assertTrue(self.tracks[0]._tache.done())
        self.writer.close.assert_called_once()
        finir_nettoyage.set()
        with self.assertRaises(asyncio.CancelledError):
            await negociation
        self.on_close.assert_awaited_once()

    async def test_annulation_pendant_sdp_ferme_le_peer(self):
        dans_sdp = asyncio.Event()

        async def attendre_sdp(_pc, _description):
            dans_sdp.set()
            await asyncio.Event().wait()

        with mock.patch.object(FauxPeer, "setRemoteDescription", attendre_sdp):
            self.reader.feed_data(b"SPS")
            negociation = asyncio.create_task(self.negocier())
            await asyncio.wait_for(dans_sdp.wait(), 1)
            negociation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await negociation

        self.assertEqual(self.peers[0].connectionState, "closed")
        self.assertTrue(self.tracks[0]._tache.done())
        self.writer.close.assert_called_once()
        self.on_close.assert_awaited_once()

    async def test_fermetures_concurrentes_attendent_un_seul_on_close(self):
        nettoyer = asyncio.Event()
        self.on_close.side_effect = nettoyer.wait
        pc = await self.demarrer()
        arrets = [asyncio.create_task(blink_webrtc.fermer_connexion(pc))
                  for _ in range(2)]
        for _ in range(100):
            if self.on_close.await_count:
                break
            await asyncio.sleep(0)
        self.on_close.assert_awaited_once()
        self.assertTrue(all(not task.done() for task in arrets))
        nettoyer.set()
        await asyncio.wait_for(asyncio.gather(*arrets), 1)
        self.assertEqual(pc.fermetures, 1)
        self.writer.close.assert_called_once()

    async def test_erreur_tcp_ne_saute_pas_la_fermeture_pc_et_on_close(self):
        pc = await self.demarrer()
        self.writer.wait_closed.side_effect = ConnectionResetError("déjà fermé")
        await asyncio.wait_for(blink_webrtc.fermer_connexion(pc), 1)
        self.assertEqual(pc.connectionState, "closed")
        self.on_close.assert_awaited_once()

    async def test_peer_bloque_ne_retient_pas_le_module_indefiniment(self):
        pc = await self.demarrer()

        async def fermeture_bloquee():
            await asyncio.Event().wait()

        with mock.patch.object(pc, "close", side_effect=fermeture_bloquee), \
             mock.patch.object(blink_webrtc, "FERMETURE_MAX_SECONDS", 0.01):
            await asyncio.wait_for(blink_webrtc.fermer_connexion(pc), 1)
        self.on_close.assert_awaited_once()

    async def test_budget_sdp_est_total_pour_les_trois_etapes(self):
        async def etape(_pc, *_args):
            await asyncio.sleep(0.04)
            return SimpleNamespace(sdp="réponse", type="answer")

        self.reader.feed_data(b"SPS")
        with mock.patch.object(blink_webrtc, "NEGOCIATION_MAX_SECONDS", 0.09), \
             mock.patch.object(FauxPeer, "setRemoteDescription", etape), \
             mock.patch.object(FauxPeer, "createAnswer", etape), \
             mock.patch.object(FauxPeer, "setLocalDescription", etape):
            with self.assertRaises(asyncio.TimeoutError):
                await self.negocier()
        self.assertEqual(self.peers[0].connectionState, "closed")
        self.on_close.assert_awaited_once()

    async def test_file_saturee_ne_perd_pas_silencieusement_de_p_frames(self):
        pc = await self.demarrer()
        with mock.patch.object(blink_webrtc, "FILE_IMAGES_MAX_OCTETS", 3):
            pc.track._mettre_unite(0, b"IDR")
            with self.assertRaisesRegex(RuntimeError, "ne consomme plus"):
                pc.track._mettre_unite(3000, b"P")
        self.assertEqual(pc.track._file.get_nowait(), (0, b"IDR"))


@unittest.skipUnless(blink_webrtc.DISPONIBLE, "aiortc optionnel non installé")
class TestsEnregistrementWebRTC(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="blink-webrtc-record-")
        self.actif = True
        self.track = blink_webrtc._PisteH264(
            asyncio.StreamReader(), ffmpeg="ffmpeg-factice",
            chemin_enregistrement=lambda: Path(self.tmp.name) / "direct.mp4",
            enregistrement_actif=lambda: self.actif,
        )
        self.track.sps_pps = b"parametres H264"
        self.track.sps_pps_pret.set()
        self.stdin = SimpleNamespace(
            write=mock.Mock(), close=mock.Mock(),
            transport=SimpleNamespace(get_write_buffer_size=lambda: 0),
        )
        self.process = SimpleNamespace(stdin=self.stdin, wait=mock.AsyncMock(),
                                       kill=mock.Mock())
        await asyncio.sleep(0)

    async def asyncTearDown(self):
        self.track.fermer()
        await asyncio.gather(self.track._tache, return_exceptions=True)
        await asyncio.sleep(0)
        self.tmp.cleanup()

    async def test_attend_idr_et_conserve_toutes_les_unites_pendant_lancement(self):
        lancement = asyncio.Event()

        async def creer(*_args, **_kwargs):
            await lancement.wait()
            return self.process

        idr = b"\x00\x00\x01\x65premiere"
        p1 = b"\x00\x00\x01\x41suivante1"
        p2 = b"\x00\x00\x01\x41suivante2"
        with mock.patch.object(blink_webrtc.asyncio, "create_subprocess_exec",
                               side_effect=creer) as creation:
            self.track._synchroniser_enregistrement(p1)
            self.assertIsNone(self.track._recorder_demarrage)
            self.track._synchroniser_enregistrement(idr)
            await asyncio.sleep(0)
            self.track._synchroniser_enregistrement(p1)
            self.track._synchroniser_enregistrement(p2)
            lancement.set()
            await self.track._recorder_demarrage

        creation.assert_awaited_once()
        self.assertEqual(self.stdin.write.call_args_list, [
            mock.call(self.track.sps_pps), mock.call(idr), mock.call(p1), mock.call(p2),
        ])

    async def test_arret_pendant_lancement_ne_ressuscite_pas_enregistreur(self):
        lancement = asyncio.Event()

        async def creer(*_args, **_kwargs):
            await lancement.wait()
            return self.process

        idr = b"\x00\x00\x01\x65image"
        with mock.patch.object(blink_webrtc.asyncio, "create_subprocess_exec",
                               side_effect=creer) as creation:
            self.track._synchroniser_enregistrement(idr)
            await asyncio.sleep(0)
            self.actif = False
            self.track._synchroniser_enregistrement(idr)
            # Un nouveau clic pendant le même lancement ne doit pas faire
            # adopter le vieux processus ou lancer deux ffmpeg concurrents.
            self.actif = True
            self.track._synchroniser_enregistrement(idr)
            lancement.set()
            await self.track._recorder_demarrage

        creation.assert_awaited_once()
        self.stdin.close.assert_called_once()
        self.stdin.write.assert_not_called()
        self.assertIsNone(self.track._recorder_process)

    async def test_enregistreur_bloque_est_arrete_sans_bloquer_le_direct(self):
        self.track._recorder_process = self.process
        self.track._recorder_stdin = self.stdin
        self.track._recorder_demande = True
        with mock.patch.object(blink_webrtc, "TAMPON_ENREGISTREMENT_MAX_OCTETS", 2):
            self.track._ecrire_enregistrement(b"trop long")
        self.stdin.close.assert_called_once()
        self.stdin.write.assert_not_called()
        self.assertFalse(self.track._tache.done())


if __name__ == "__main__":
    unittest.main()
