"""Le reveil partage le module avec le direct et confirme la commande Blink."""

from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-camera-wakeup-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import serve  # noqa: E402 - environnement isole avant import
from blinkpy import api  # noqa: E402


class ReveilCameraTests(unittest.TestCase):
    def setUp(self):
        self.handler = serve.Handler.__new__(serve.Handler)
        self.slot = threading.BoundedSemaphore(1)
        self.info = {}
        self.blink = object()
        self.camera = SimpleNamespace(
            network_id=42,
            snap_picture=mock.AsyncMock(return_value={"id": 123, "network_id": 42}),
        )
        self.status = mock.AsyncMock(return_value={"status_code": 908, "complete": True})
        self.disk = tempfile.TemporaryDirectory(prefix="blink-wakeup-lock-")
        self.addCleanup(self.disk.cleanup)
        self.root = Path(self.disk.name)

        def reserver(_owner, **_kwargs):
            return serve.runtime.verrou("hub", "reveil", racine=self.root)

        def appeler(factory, timeout):
            return asyncio.run(factory(self.blink))

        self.call = mock.Mock(side_effect=appeler)
        self.hub_lock = mock.Mock(side_effect=reserver)
        for patch in (
            mock.patch.object(serve, "MODULE_SLOT", self.slot),
            mock.patch.object(serve, "MODULE_SLOT_INFO", self.info),
            mock.patch.object(serve.blink_engine, "hub_lock", self.hub_lock),
            mock.patch.object(serve.BLINK, "call", self.call),
            mock.patch.object(serve.BLINK, "find_camera", return_value=(None, self.camera)),
            mock.patch.object(api, "request_command_status", self.status),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def verifier_liberation(self):
        self.assertEqual(self.info, {})
        self.assertTrue(self.slot.acquire(blocking=False), "slot memoire encore occupe")
        self.slot.release()
        with serve.runtime.verrou("hub", "verification", racine=self.root):
            pass

    def test_direct_actif_refuse_le_reveil_sans_appeler_blink(self):
        self.slot.acquire()
        self.info.update({"quoi": "direct WebRTC", "camera": "Salon"})
        try:
            with self.assertRaisesRegex(serve.blink_engine.BusyError, "Salon"):
                self.handler.reveiller_camera("Jardin")
            self.call.assert_not_called()
            self.hub_lock.assert_not_called()
            self.assertEqual(self.info["camera"], "Salon")
            self.assertFalse(self.slot.acquire(blocking=False))
        finally:
            self.slot.release()

    def test_telechargement_actif_refuse_le_reveil_et_rend_le_slot(self):
        with serve.runtime.verrou("hub", "telechargement", racine=self.root):
            with self.assertRaises(serve.blink_engine.BusyError):
                self.handler.reveiller_camera("Jardin")
        self.call.assert_not_called()
        self.verifier_liberation()

    def test_une_photo_et_confirmation_gardent_les_deux_verrous(self):
        async def confirmer(*_args):
            self.assertFalse(self.slot.acquire(blocking=False))
            self.assertEqual(self.info["camera"], "Salon")
            with self.assertRaises(serve.runtime.BusyError):
                with serve.runtime.verrou("hub", "telechargement", racine=self.root):
                    self.fail("Le telechargement a pu prendre le module pendant le reveil")
            return {"status_code": 908, "complete": True}

        self.status.side_effect = confirmer
        self.handler.reveiller_camera("Salon")
        self.camera.snap_picture.assert_awaited_once_with()
        self.status.assert_awaited_once_with(self.blink, 42, 123)
        self.hub_lock.assert_called_once_with("reveil", attente=serve.ATTENTE_HUB_MAX_SECONDS)
        self.verifier_liberation()

    def test_reponse_refusee_ne_devient_pas_un_succes(self):
        for reponse in (None, False, {}, {"message": "System is busy"}):
            with self.subTest(reponse=reponse):
                self.camera.snap_picture.return_value = reponse
                with self.assertRaisesRegex(RuntimeError, "refuse"):
                    self.handler.reveiller_camera("Salon")
                self.verifier_liberation()
        self.status.assert_not_awaited()

    def test_commande_creee_mais_non_confirmee_ne_devient_pas_un_succes(self):
        for statut in (None, {}, {"status_code": 908, "complete": False},
                       {"status_code": 101, "complete": True}):
            with self.subTest(statut=statut):
                self.status.return_value = statut
                with self.assertRaisesRegex(RuntimeError, "pas confirme"):
                    self.handler.reveiller_camera("Salon")
                self.verifier_liberation()

    def test_exception_ou_annulation_rend_les_deux_verrous(self):
        for erreur in (OSError("reseau coupe"), asyncio.CancelledError()):
            with self.subTest(erreur=type(erreur).__name__):
                self.camera.snap_picture.side_effect = erreur
                with self.assertRaises(type(erreur)):
                    self.handler.reveiller_camera("Salon")
                self.verifier_liberation()

    def test_erreur_liberation_disque_ne_fait_pas_fuir_le_slot(self):
        verrou = mock.MagicMock()
        verrou.__exit__.side_effect = OSError("verrou illisible")
        self.hub_lock.side_effect = None
        self.hub_lock.return_value = verrou
        with self.assertRaisesRegex(OSError, "verrou illisible"):
            self.handler.reveiller_camera("Salon")
        self.verifier_liberation()

    def test_route_distingue_module_occupe_et_refus_blink(self):
        for erreur, code in ((serve.blink_engine.BusyError("occupe"), 409),
                             (RuntimeError("refus Blink"), 503)):
            with self.subTest(code=code):
                body = json.dumps({"name": "Salon"}).encode()
                self.handler.path = "/api/reveiller"
                self.handler.headers = {"Content-Length": str(len(body))}
                self.handler.rfile = io.BytesIO(body)
                self.handler.hote_autorise = lambda: True
                self.handler.jeton_valide = lambda: True
                self.handler.reveiller_camera = mock.Mock(side_effect=erreur)
                self.handler.system_state = mock.Mock()
                self.handler.send_json = mock.Mock()
                self.handler.do_POST()
                self.assertEqual(self.handler.send_json.call_args.args[1], code)
                self.handler.system_state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
