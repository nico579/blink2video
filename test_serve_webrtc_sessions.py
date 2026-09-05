"""Sessions Live View isolées : aucun compte Blink ni caméra réelle."""

import asyncio
import concurrent.futures
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-session-live-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import serve  # noqa: E402
from blinkpy import api  # noqa: E402


SESSION_A = "a" * 32
SESSION_B = "b" * 32


class TestsSessionsDirect(unittest.TestCase):
    def setUp(self):
        self.session = serve.BlinkSession()
        self.session.blink = object()
        self.slot = threading.BoundedSemaphore(1)
        self.verrou = mock.MagicMock()
        self.flux = SimpleNamespace(
            start=mock.AsyncMock(), feed=mock.AsyncMock(), stop=mock.Mock(),
            url="tcp://127.0.0.1:1234", command_id=123,
            camera=SimpleNamespace(network_id=456, sync=SimpleNamespace(blink=object())),
        )
        self.camera = SimpleNamespace(name="Camera factice", init_livestream=mock.AsyncMock(return_value=self.flux))
        self.pc = None
        self.nettoyage = None
        self.stack = contextlib.ExitStack()
        for patch in (
            mock.patch.object(serve, "BLINK", self.session),
            mock.patch.object(serve, "MODULE_SLOT", self.slot),
            mock.patch.object(serve, "_journal_direct"),
            mock.patch.object(serve.blink_engine, "hub_lock", return_value=self.verrou),
            mock.patch.object(self.session, "find_camera", return_value=(object(), self.camera)),
            mock.patch.object(serve.blink_webrtc, "negocier", side_effect=self.negocier),
            mock.patch.object(api, "request_command_done", new=mock.AsyncMock(return_value={})),
        ):
            self.stack.enter_context(patch)
        serve.DIRECT_WEBRTC_SESSION.clear()
        serve.DIRECT_ARRETS_RECENTS.clear()
        serve.MODULE_SLOT_INFO.clear()
        serve.ENREGISTREMENT_DIRECT_ACTIF.clear()

    def tearDown(self):
        holder = serve.DIRECT_WEBRTC_SESSION.get("session")
        if holder:
            serve._demander_arret_direct(holder["session_id"])
        if self.session.loop:
            async def terminer():
                for _ in range(100):
                    if not serve.DIRECT_WEBRTC_SESSION:
                        break
                    await asyncio.sleep(0.001)
                tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            asyncio.run_coroutine_threadsafe(terminer(), self.session.loop).result(2)
            self.session.loop.call_soon_threadsafe(self.session.loop.stop)
            echeance = time.monotonic() + 2
            while self.session.loop.is_running() and time.monotonic() < echeance:
                time.sleep(0.001)
            self.session.loop.close()
        self.stack.close()
        serve.DIRECT_WEBRTC_SESSION.clear()
        serve.DIRECT_ARRETS_RECENTS.clear()
        serve.MODULE_SLOT_INFO.clear()

    async def negocier(self, _url, _sdp, _type, on_close, **_kwargs):
        self.nettoyage = on_close
        self.pc = SimpleNamespace(connectionState="connected")

        async def close():
            self.pc.connectionState = "closed"
            await on_close()
        self.pc.close = mock.AsyncMock(side_effect=close)
        return self.pc, "answer-sdp", "answer"

    def handler(self):
        handler = serve.Handler.__new__(serve.Handler)
        handler.ffmpeg = "ffmpeg-factice"
        handler.send_json = mock.Mock()
        return handler

    def ouvrir(self, handler=None, session_id=SESSION_A):
        handler = handler or self.handler()
        handler.send_offer_webrtc("Camera factice", {
            "sdp": "v=0\r\n", "type": "offer", "session_id": session_id,
        })
        return handler

    def attendre_fin(self):
        self.assertTrue(self.slot.acquire(timeout=2), "Le module n'a pas été rendu")
        self.assertFalse(self.slot.acquire(blocking=False), "Le module a été rendu deux fois")
        self.slot.release()
        self.assertEqual(serve.DIRECT_WEBRTC_SESSION, {})

    def post(self, path, payload):
        handler = self.handler()
        body = json.dumps(payload).encode()
        handler.path = path
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.hote_autorise = lambda: True
        handler.jeton_valide = lambda: True
        handler.do_POST()
        return handler.send_json.call_args

    def test_arret_cible_et_ancien_arret_ne_touchent_pas_la_session_suivante(self):
        handler = self.ouvrir()
        self.assertEqual(handler.send_json.call_args.args[0]["session_id"], SESSION_A)
        self.assertFalse(serve._demander_arret_direct(SESSION_B))
        self.assertEqual(self.pc.connectionState, "connected")
        self.assertTrue(serve._demander_arret_direct(SESSION_A))
        self.attendre_fin()
        self.ouvrir(session_id="c" * 32)
        self.assertFalse(serve._demander_arret_direct(SESSION_A))
        self.assertEqual(self.pc.connectionState, "connected")
        serve._demander_arret_direct("c" * 32)
        self.attendre_fin()

    def test_arret_avant_offre_empeche_le_reveil(self):
        self.assertFalse(serve._demander_arret_direct(SESSION_A))
        handler = self.ouvrir()
        self.camera.init_livestream.assert_not_awaited()
        self.assertEqual(handler.send_json.call_args.args[1], 409)
        self.attendre_fin()

    def test_offre_sans_identifiant_ne_reveille_pas(self):
        handler = self.ouvrir(session_id="")
        self.assertEqual(handler.send_json.call_args.args[1], 400)
        self.camera.init_livestream.assert_not_awaited()

    def test_arret_sans_identifiant_est_refuse_sans_couper_le_direct(self):
        self.ouvrir()
        result = self.post("/api/arreter-direct", {})
        self.assertEqual(result.args[1], 400)
        self.assertEqual(self.pc.connectionState, "connected")

    def test_erreur_stop_et_erreur_verrou_ne_font_pas_fuir_le_module(self):
        self.flux.stop.side_effect = OSError("socket ferme")
        self.verrou.__exit__.side_effect = OSError("verrou illisible")
        self.ouvrir()
        serve._demander_arret_direct(SESSION_A)
        self.attendre_fin()
        self.flux.stop.assert_called_once()
        self.verrou.__exit__.assert_called_once()
        api.request_command_done.assert_awaited_once()

    def test_echec_start_envoie_done_meme_sans_feed(self):
        self.flux.start.side_effect = RuntimeError("demarrage impossible")
        handler = self.ouvrir()
        self.assertEqual(handler.send_json.call_args.args[1], 503)
        self.flux.feed.assert_not_called()
        api.request_command_done.assert_awaited_once()
        self.attendre_fin()

    def test_echec_feed_avant_poll_envoie_done(self):
        self.flux.feed.side_effect = ConnectionError("relais indisponible")
        self.ouvrir()
        serve._demander_arret_direct(SESSION_A)
        self.attendre_fin()
        api.request_command_done.assert_awaited_once()

    def test_echec_relais_interrompt_l_attente_sps_sans_attendre_quarante_secondes(self):
        self.flux.feed.side_effect = ConnectionError("relais indisponible")
        annulee = threading.Event()

        async def sans_sps(*_args, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                annulee.set()

        serve.blink_webrtc.negocier.side_effect = sans_sps
        debut = time.monotonic()
        handler = self.ouvrir()
        self.assertLess(time.monotonic() - debut, 2)
        self.assertTrue(annulee.is_set())
        self.assertEqual(handler.send_json.call_args.args[1], 503)
        api.request_command_done.assert_awaited_once()
        self.attendre_fin()

    def test_annulation_pendant_le_reveil_ne_depend_pas_du_verrou_blink(self):
        commence = threading.Event()

        async def reveil():
            commence.set()
            await asyncio.Event().wait()
        self.camera.init_livestream.side_effect = reveil
        handler = self.handler()
        thread = threading.Thread(target=self.ouvrir, args=(handler,))
        thread.start()
        try:
            self.assertTrue(commence.wait(2))
            self.assertTrue(serve._demander_arret_direct(SESSION_A))
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.attendre_fin()
            serve.blink_webrtc.negocier.assert_not_called()
        finally:
            thread.join(2)

    def test_client_deconnecte_avant_reponse_sdp_ferme_sa_session(self):
        handler = self.handler()
        handler.send_json.side_effect = BrokenPipeError()
        self.ouvrir(handler)
        self.attendre_fin()
        self.pc.close.assert_awaited_once()

    def test_nettoyage_concurrent_est_attendu_une_seule_fois(self):
        self.ouvrir()
        async def fermer_deux_fois():
            await asyncio.gather(self.nettoyage(), self.nettoyage())
        asyncio.run_coroutine_threadsafe(fermer_deux_fois(), self.session.loop).result(2)
        self.attendre_fin()
        self.flux.stop.assert_called_once()
        self.verrou.__exit__.assert_called_once()

    def test_enregistrement_valide_session_et_conserve_compatibilite_mse(self):
        self.ouvrir()
        result = self.post("/api/direct-enregistrement", {"actif": True, "session_id": SESSION_B})
        self.assertEqual(result.args[1], 409)
        self.assertFalse(serve.ENREGISTREMENT_DIRECT_ACTIF.is_set())
        self.post("/api/direct-enregistrement", {"actif": True, "session_id": SESSION_A})
        self.assertTrue(serve.ENREGISTREMENT_DIRECT_ACTIF.is_set())
        serve._demander_arret_direct(SESSION_A)
        self.attendre_fin()
        serve.MODULE_SLOT_INFO["quoi"] = "direct MSE"
        self.post("/api/direct-enregistrement", {"actif": True})
        self.assertTrue(serve.ENREGISTREMENT_DIRECT_ACTIF.is_set())


class TestsBudgetSession(unittest.TestCase):
    def test_attente_du_verrou_est_comprise_dans_le_timeout(self):
        session = serve.BlinkSession()
        session.lock.acquire()
        try:
            debut = time.monotonic()
            with self.assertRaises(concurrent.futures.TimeoutError):
                session.call(lambda _b: None, timeout=0.01)
            self.assertLess(time.monotonic() - debut, 0.5)
            self.assertIsNone(session.loop)
        finally:
            session.lock.release()


if __name__ == "__main__":
    unittest.main()
