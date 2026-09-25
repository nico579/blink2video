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
        self.blink = SimpleNamespace(get_homescreen=mock.AsyncMock())
        self.media = mock.AsyncMock(return_value=SimpleNamespace(
            status=200, read=mock.AsyncMock(return_value=b"\xff\xd8\xff\xe0photo")))
        self.camera = SimpleNamespace(
            name="Jardin",
            camera_id="7",
            network_id=42,
            thumbnail="ancienne-url",
            _cached_image=b"ancienne photo",
            snap_picture=mock.AsyncMock(return_value={"id": 123, "network_id": 42}),
            get_media=self.media,
        )
        async def changer_vignette(info):
            self.camera.thumbnail = info["thumbnail"]
        self.camera.update_images = mock.AsyncMock(side_effect=changer_vignette)
        self.numero_vignette = 0
        async def info_camera(_camera_id, **_kwargs):
            self.numero_vignette += 1
            return {"thumbnail": f"nouvelle-url-{self.numero_vignette}"}
        self.sync = SimpleNamespace(
            network_id=42,
            get_unique_info=mock.Mock(return_value={"thumbnail": "nouvelle-url"}),
            get_camera_info=mock.AsyncMock(side_effect=info_camera),
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
            mock.patch.object(serve.BLINK, "find_camera", return_value=(self.sync, self.camera)),
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
        cle = serve.camera_key(self.sync, self.camera.name, self.camera)
        vignette = self.thumbs / "cameras" / f"{serve.safe_file(cle)}.jpg"
        self.assertTrue(vignette.is_file(), "la vignette du Direct doit aussi être mise à jour")
        self.assertEqual(vignette.read_bytes(), b"\xff\xd8\xff\xe0photo")
        self.verifier_liberation()

    def test_adresse_change_apres_plusieurs_lectures(self):
        self.sync.get_camera_info.side_effect = [
            {"thumbnail": "ancienne-url"},
            {"thumbnail": "ancienne-url"},
            {"thumbnail": "nouvelle-url"},
        ]

        async def media_courant():
            corps = (b"\xff\xd8\xff\xe0photo" if self.camera.thumbnail == "nouvelle-url"
                     else b"ancienne photo")
            return SimpleNamespace(status=200, read=mock.AsyncMock(return_value=corps))

        self.media.side_effect = media_courant
        with mock.patch.object(serve.asyncio, "sleep", new_callable=mock.AsyncMock) as pause:
            chemin = self.handler.declencher_snapshot("Jardin")
        self.assertEqual(chemin.read_bytes(), b"\xff\xd8\xff\xe0photo")
        self.assertEqual(self.sync.get_camera_info.await_count, 3)
        self.assertEqual(pause.await_count, 2)
        self.verifier_liberation()

    def test_vignette_rafraichie_depuis_blink_sur_demande(self):
        cle = serve.camera_key(self.sync, self.camera.name, self.camera)
        cache = self.thumbs / "cameras" / f"{serve.safe_file(cle)}.jpg"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"ancienne photo")
        self.handler.wfile = io.BytesIO()
        self.handler.send_response = mock.Mock()
        self.handler.send_header = mock.Mock()
        self.handler.end_headers = mock.Mock()

        self.handler.send_camera_thumb(cle)
        self.assertEqual(self.handler.wfile.getvalue(), b"ancienne photo")
        self.media.assert_not_awaited()

        self.handler.wfile = io.BytesIO()
        self.handler.send_camera_thumb(cle, refresh=True)
        self.blink.get_homescreen.assert_awaited_once_with()
        self.camera.update_images.assert_awaited_once()
        self.media.assert_awaited_once_with()
        self.assertEqual(cache.read_bytes(), b"\xff\xd8\xff\xe0photo")
        self.assertEqual(self.handler.wfile.getvalue(), cache.read_bytes())

    def test_vignette_rafraichie_conserve_cache_si_blink_echoue(self):
        cle = serve.camera_key(self.sync, self.camera.name, self.camera)
        cache = self.thumbs / "cameras" / f"{serve.safe_file(cle)}.jpg"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"ancienne photo")
        self.handler.wfile = io.BytesIO()
        self.handler.send_response = mock.Mock()
        self.handler.send_header = mock.Mock()
        self.handler.end_headers = mock.Mock()
        self.media.side_effect = RuntimeError("Blink indisponible")

        self.handler.send_camera_thumb(cle, refresh=True)
        self.assertEqual(cache.read_bytes(), b"ancienne photo")
        self.assertEqual(self.handler.wfile.getvalue(), b"ancienne photo")

    def test_vignette_ecrite_sous_la_cle_stable_pas_l_identite_fournie(self):
        # Le webhook (issue GitHub #9) reçoit le nom affiché de la caméra
        # (gabarit "NOM_CAMERA" des réglages), jamais la clé opaque que la
        # tuile du Direct utilise pour sa propre vignette (c.key côté JS) :
        # écrire sous l'identité reçue plutôt que sous cette clé stable
        # laisserait la tuile inchangée pour tout appel webhook (constaté en
        # conditions réelles avec une vraie caméra, avant ce correctif).
        self.handler.declencher_snapshot("Jardin")
        cle = serve.camera_key(self.sync, self.camera.name, self.camera)
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
        with mock.patch.object(serve.asyncio, "sleep", new_callable=mock.AsyncMock):
            for reponse in (None, SimpleNamespace(status=404)):
                with self.subTest(reponse=reponse):
                    self.media.return_value = reponse
                    with self.assertRaisesRegex(RuntimeError, "publi"):
                        self.handler.declencher_snapshot("Jardin")
                    self.verifier_liberation()
        self.assertFalse(any(self.snapshots.rglob("*.jpg")),
                         "aucun fichier ne doit exister sans image recuperee")
        self.assertFalse(any(self.thumbs.rglob("*.jpg")),
                         "la vignette du Direct ne doit pas non plus changer sans image")

    def test_photo_ancienne_n_est_pas_enregistree_comme_nouvelle(self):
        cle = serve.camera_key(self.sync, self.camera.name, self.camera)
        vignette = self.thumbs / "cameras" / f"{serve.safe_file(cle)}.jpg"
        vignette.parent.mkdir(parents=True)
        vignette.write_bytes(b"ancienne photo")
        self.sync.get_camera_info.side_effect = None
        self.sync.get_camera_info.return_value = {"thumbnail": "ancienne-url"}
        self.media.return_value = SimpleNamespace(
            status=200, read=mock.AsyncMock(return_value=b"ancienne photo"))
        with mock.patch.object(serve.asyncio, "sleep", new_callable=mock.AsyncMock) as pause:
            with self.assertRaisesRegex(RuntimeError, "publi"):
                self.handler.declencher_snapshot("Jardin")
        self.assertEqual(self.sync.get_camera_info.await_count, 10)
        self.assertEqual(self.media.await_count, 10)
        self.assertEqual(pause.await_count, 9)
        self.assertFalse(any(self.snapshots.rglob("*.jpg")))
        self.assertEqual(vignette.read_bytes(), b"ancienne photo")
        self.verifier_liberation()

    def test_lecture_bloquee_respecte_le_delai_et_libere_le_module(self):
        async def info_bloquee(_camera_id, **_kwargs):
            await asyncio.sleep(3600)

        self.sync.get_camera_info.side_effect = info_bloquee
        with mock.patch.object(serve, "ATTENTE_MEDIA_SNAPSHOT_SECONDS", 1):
            with self.assertRaisesRegex(RuntimeError, "publi"):
                self.handler.declencher_snapshot("Jardin")
        self.assertFalse(any(self.snapshots.rglob("*.jpg")))
        self.assertFalse(any(self.thumbs.rglob("*.jpg")))
        self.verifier_liberation()

    def test_photo_nouvelle_acceptee_avec_url_stable(self):
        self.sync.get_camera_info.side_effect = None
        self.sync.get_camera_info.return_value = {"thumbnail": "ancienne-url"}
        chemin = self.handler.declencher_snapshot("Jardin")
        self.assertEqual(chemin.read_bytes(), b"\xff\xd8\xff\xe0photo")
        self.assertEqual(self.sync.get_camera_info.await_count, 1)

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
