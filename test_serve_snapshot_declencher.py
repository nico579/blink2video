"""Le declenchement d'une photo a la demande (issue GitHub #9) partage le
module et la confirmation de commande avec reveiller_camera(), et ajoute la
recuperation + sauvegarde de l'image resultante."""

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
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-snapshot-declencher-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import serve  # noqa: E402 - environnement isole avant import
from blinkpy import api  # noqa: E402


class DeclencherSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.handler = serve.Handler.__new__(serve.Handler)
        self.snapshots = Path(tempfile.mkdtemp(prefix="blink-snapshots-"))
        self.thumbs = Path(tempfile.mkdtemp(prefix="blink-thumbs-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.snapshots, ignore_errors=True))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.thumbs, ignore_errors=True))
        self.handler.paths = {"snapshots": self.snapshots, "thumbs": self.thumbs}
        self.slot = threading.BoundedSemaphore(1)
        self.info = {}
        self.blink = object()
        self.media = mock.AsyncMock(return_value=SimpleNamespace(
            status=200, read=mock.AsyncMock(return_value=b"\xff\xd8\xff\xe0photo")))
        self.camera = SimpleNamespace(
            name="Jardin",
            network_id=42,
            snap_picture=mock.AsyncMock(return_value={"id": 123, "network_id": 42}),
            get_media=self.media,
        )
        self.status = mock.AsyncMock(return_value={"status_code": 908, "complete": True})
        self.disk = tempfile.TemporaryDirectory(prefix="blink-snapshot-lock-")
        self.addCleanup(self.disk.cleanup)
        self.root = Path(self.disk.name)

        def reserver(_owner, **_kwargs):
            return serve.runtime.verrou("hub", "snapshot", racine=self.root)

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

    def test_photo_confirmee_est_recuperee_et_sauvee(self):
        chemin = self.handler.declencher_snapshot("Jardin")
        self.camera.snap_picture.assert_awaited_once_with()
        self.status.assert_awaited_once_with(self.blink, 42, 123)
        self.media.assert_awaited_once_with()
        self.assertTrue(chemin.is_file())
        self.assertEqual(chemin.read_bytes(), b"\xff\xd8\xff\xe0photo")
        self.assertEqual(chemin.parent.name, "Jardin")
        self.assertTrue(chemin.name.endswith(".jpg"))
        cle = serve.camera_key(None, self.camera.name, self.camera)
        vignette = self.thumbs / "cameras" / f"{serve.safe_file(cle)}.jpg"
        self.assertTrue(vignette.is_file(), "la vignette du Direct doit aussi être mise à jour")
        self.assertEqual(vignette.read_bytes(), b"\xff\xd8\xff\xe0photo")
        self.verifier_liberation()

    def test_vignette_ecrite_sous_la_cle_stable_pas_l_identite_fournie(self):
        # Le webhook (issue GitHub #9) reçoit le nom affiché de la caméra
        # (gabarit "NOM_CAMERA" des réglages), jamais la clé opaque que la
        # tuile du Direct utilise pour sa propre vignette (c.key côté JS) :
        # écrire sous l'identité reçue plutôt que sous cette clé stable
        # laisserait la tuile inchangée pour tout appel webhook (constaté en
        # conditions réelles avec une vraie caméra, avant ce correctif).
        self.handler.declencher_snapshot("Jardin")
        cle = serve.camera_key(None, self.camera.name, self.camera)
        self.assertNotEqual(cle, "Jardin", "la clé opaque doit différer du nom pour que ce test soit probant")
        vignette_par_cle = self.thumbs / "cameras" / f"{serve.safe_file(cle)}.jpg"
        vignette_par_nom = self.thumbs / "cameras" / "Jardin.jpg"
        self.assertTrue(vignette_par_cle.is_file())
        self.assertFalse(vignette_par_nom.is_file())

    def test_deux_photos_distinctes_produisent_deux_fichiers(self):
        # Deux vraies photos distinctes (jamais strictement les memes octets
        # en pratique, ne serait-ce que par le bruit du capteur) doivent
        # produire deux fichiers, meme prises dans la meme seconde.
        self.media.return_value = SimpleNamespace(
            status=200, read=mock.AsyncMock(return_value=b"\xff\xd8\xff\xe0premiere"))
        premier = self.handler.declencher_snapshot("Jardin")
        self.media.return_value = SimpleNamespace(
            status=200, read=mock.AsyncMock(return_value=b"\xff\xd8\xff\xe0seconde"))
        second = self.handler.declencher_snapshot("Jardin")
        self.assertNotEqual(premier, second, "deux photos ne doivent pas s'ecraser")

    def test_meme_contenu_dans_la_meme_seconde_ne_perd_aucun_appel(self):
        # Le cas improbable (memes octets, meme seconde) ne doit pas non
        # plus lever d'erreur : rien n'exige un fichier par photo distincte,
        # seulement qu'aucune donnee ne disparaisse en silence.
        premier = self.handler.declencher_snapshot("Jardin")
        second = self.handler.declencher_snapshot("Jardin")
        self.assertTrue(premier.is_file())
        self.assertTrue(second.is_file())

    def test_module_occupe_refuse_sans_appeler_blink(self):
        self.slot.acquire()
        self.info.update({"quoi": "direct WebRTC", "camera": "Salon"})
        try:
            with self.assertRaisesRegex(serve.blink_engine.BusyError, "Salon"):
                self.handler.declencher_snapshot("Jardin")
            self.call.assert_not_called()
        finally:
            self.slot.release()

    def test_reponse_refusee_ne_devient_pas_un_succes(self):
        for reponse in (None, False, {}, {"message": "System is busy"}):
            with self.subTest(reponse=reponse):
                self.camera.snap_picture.return_value = reponse
                with self.assertRaisesRegex(RuntimeError, "refus"):
                    self.handler.declencher_snapshot("Jardin")
                self.verifier_liberation()
        self.status.assert_not_awaited()

    def test_commande_non_confirmee_ne_devient_pas_un_succes(self):
        for statut in (None, {}, {"status_code": 908, "complete": False},
                       {"status_code": 101, "complete": True}):
            with self.subTest(statut=statut):
                self.status.return_value = statut
                with self.assertRaisesRegex(RuntimeError, "confirm"):
                    self.handler.declencher_snapshot("Jardin")
                self.verifier_liberation()
        self.media.assert_not_awaited()

    def test_image_indisponible_apres_confirmation(self):
        for reponse in (None, SimpleNamespace(status=404)):
            with self.subTest(reponse=reponse):
                self.media.return_value = reponse
                with self.assertRaisesRegex(RuntimeError, "[Ii]mage"):
                    self.handler.declencher_snapshot("Jardin")
                self.verifier_liberation()
        self.assertFalse(any(self.snapshots.rglob("*.jpg")),
                         "aucun fichier ne doit exister sans image recuperee")
        self.assertFalse(any(self.thumbs.rglob("*.jpg")),
                         "la vignette du Direct ne doit pas non plus changer sans image")

    def test_exception_reseau_rend_les_deux_verrous(self):
        self.camera.snap_picture.side_effect = OSError("reseau coupe")
        with self.assertRaises(OSError):
            self.handler.declencher_snapshot("Jardin")
        self.verifier_liberation()

    def test_route_manuelle_distingue_module_occupe_et_refus_blink(self):
        for erreur, code in ((serve.blink_engine.BusyError("occupe"), 409),
                             (RuntimeError("refus Blink"), 503)):
            with self.subTest(code=code):
                body = json.dumps({"name": "Jardin"}).encode()
                self.handler.path = "/api/snapshot"
                self.handler.headers = {"Content-Length": str(len(body))}
                self.handler.rfile = io.BytesIO(body)
                self.handler.hote_autorise = lambda: True
                self.handler.jeton_valide = lambda: True
                self.handler.declencher_snapshot = mock.Mock(side_effect=erreur)
                self.handler.send_json = mock.Mock()
                self.handler.do_POST()
                self.assertEqual(self.handler.send_json.call_args.args[1], code)


if __name__ == "__main__":
    unittest.main()
