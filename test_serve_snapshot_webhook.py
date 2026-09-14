"""Le webhook de snapshot (issue GitHub #9) a sa propre authentification,
independante de hote_autorise()/jeton_valide() (le modele du navigateur ne
s'applique pas a un appelant externe) : un secret persistant compare en
temps constant. Couvre aussi resolve_snapshot() (meme garde-fou anti-
traversee que resolve_media()), lister_snapshots() et la suppression."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-snapshot-webhook-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import runtime  # noqa: E402 - environnement isole avant import
import serve  # noqa: E402


class JetonWebhookTests(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory(prefix="blink-jeton-webhook-")
        self.addCleanup(self.dossier.cleanup)
        self._ancien = os.environ.get("BLINK_HOME")
        os.environ["BLINK_HOME"] = self.dossier.name

    def tearDown(self):
        if self._ancien is None:
            os.environ.pop("BLINK_HOME", None)
        else:
            os.environ["BLINK_HOME"] = self._ancien

    def test_genere_et_persiste_au_premier_appel(self):
        jeton = runtime.lire_jeton_webhook()
        self.assertGreaterEqual(len(jeton), 32)
        self.assertEqual(runtime.lire_jeton_webhook(), jeton, "doit rester stable entre deux lectures")

    def test_regenerer_invalide_l_ancien(self):
        ancien = runtime.lire_jeton_webhook()
        nouveau = runtime.regenerer_jeton_webhook()
        self.assertNotEqual(ancien, nouveau)
        self.assertEqual(runtime.lire_jeton_webhook(), nouveau)


class ResolveSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.handler = serve.Handler.__new__(serve.Handler)
        self.racine = Path(tempfile.mkdtemp(prefix="blink-resolve-snapshot-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.racine, ignore_errors=True))
        self.handler.paths = {"snapshots": self.racine}
        (self.racine / "Jardin").mkdir()
        (self.racine / "Jardin" / "2026-09-14_10-00-00Z_abcd1234.jpg").write_bytes(b"photo")

    def test_fichier_existant_resolu(self):
        chemin = self.handler.resolve_snapshot("Jardin/2026-09-14_10-00-00Z_abcd1234.jpg")
        self.assertIsNotNone(chemin)
        self.assertTrue(chemin.is_file())

    def test_traversee_de_chemin_refusee(self):
        for tentative in ("../secret.jpg", "Jardin/../../secret.jpg",
                         "Jardin/../autre/x.jpg"):
            with self.subTest(tentative=tentative):
                self.assertIsNone(self.handler.resolve_snapshot(tentative))

    def test_mauvaise_extension_refusee(self):
        self.assertIsNone(self.handler.resolve_snapshot("Jardin/fichier.mp4"))
        self.assertIsNone(self.handler.resolve_snapshot("Jardin/fichier.txt"))

    def test_fichier_absent_renvoie_none(self):
        self.assertIsNone(self.handler.resolve_snapshot("Jardin/absent.jpg"))

    def test_forme_sans_sous_dossier_refusee(self):
        self.assertIsNone(self.handler.resolve_snapshot("fichier.jpg"))


class ListerSnapshotsTests(unittest.TestCase):
    def setUp(self):
        self.handler = serve.Handler.__new__(serve.Handler)
        self.racine = Path(tempfile.mkdtemp(prefix="blink-lister-snapshot-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.racine, ignore_errors=True))
        self.handler.paths = {"snapshots": self.racine}

    def _creer(self, camera: str, horodatage: str) -> None:
        dossier = self.racine / camera
        dossier.mkdir(parents=True, exist_ok=True)
        (dossier / f"{horodatage}.jpg").write_bytes(b"photo")

    def test_dossier_absent_renvoie_liste_vide(self):
        self.handler.paths = {"snapshots": self.racine / "n-existe-pas"}
        self.assertEqual(self.handler.lister_snapshots(), [])

    def test_tri_du_plus_recent_au_plus_ancien_toutes_cameras_confondues(self):
        self._creer("Jardin", "2026-09-14_08-00-00Z_aaaa")
        self._creer("Salon", "2026-09-14_10-00-00Z_bbbb")
        self._creer("Jardin", "2026-09-14_09-00-00Z_cccc")
        horodatages = [entree["horodatage"] for entree in self.handler.lister_snapshots()]
        self.assertEqual(horodatages, sorted(horodatages, reverse=True))
        self.assertEqual(horodatages[0], "2026-09-14_10-00-00Z_bbbb")

    def test_chemin_relatif_pret_pour_l_url(self):
        self._creer("Jardin", "2026-09-14_08-00-00Z_aaaa")
        entree = self.handler.lister_snapshots()[0]
        self.assertEqual(entree["fichier"], "Jardin/2026-09-14_08-00-00Z_aaaa.jpg")
        self.assertEqual(entree["camera"], "Jardin")


class WebhookAuthTests(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory(prefix="blink-webhook-auth-")
        self.addCleanup(self.dossier.cleanup)
        self._ancien = os.environ.get("BLINK_HOME")
        os.environ["BLINK_HOME"] = self.dossier.name
        self.jeton = runtime.regenerer_jeton_webhook()
        self.handler = serve.Handler.__new__(serve.Handler)
        self.handler.paths = {"snapshots": Path(self.dossier.name) / "Blink_Snapshots"}
        self.handler.send_json = mock.Mock()
        self.handler.send_error = mock.Mock()
        self.declenche = mock.Mock(
            return_value=self.handler.paths["snapshots"] / "Jardin" / "x.jpg")
        self.handler.declencher_snapshot = self.declenche

    def tearDown(self):
        if self._ancien is None:
            os.environ.pop("BLINK_HOME", None)
        else:
            os.environ["BLINK_HOME"] = self._ancien

    def appeler(self, query: str):
        self.handler.path = f"{serve.WEBHOOK_SNAPSHOT_ROUTE}?{query}"
        self.handler.gerer_webhook_snapshot()

    def test_jeton_correct_declenche_la_photo(self):
        (self.handler.paths["snapshots"] / "Jardin").mkdir(parents=True)
        (self.handler.paths["snapshots"] / "Jardin" / "x.jpg").write_bytes(b"photo")
        self.appeler(f"camera=Jardin&token={self.jeton}")
        self.declenche.assert_called_once_with("Jardin")
        self.handler.send_error.assert_not_called()
        self.assertTrue(self.handler.send_json.call_args.args[0]["ok"])

    def test_jeton_absent_refuse_sans_toucher_a_blink(self):
        self.appeler("camera=Jardin")
        self.handler.send_error.assert_called_once_with(403)
        self.declenche.assert_not_called()

    def test_jeton_incorrect_refuse(self):
        self.appeler("camera=Jardin&token=mauvais-jeton-devine")
        self.handler.send_error.assert_called_once_with(403)
        self.declenche.assert_not_called()

    def test_jeton_correct_mais_camera_manquante(self):
        self.appeler(f"token={self.jeton}")
        self.declenche.assert_not_called()
        self.assertEqual(self.handler.send_json.call_args.args[1], 400)

    def test_camera_occupee_renvoie_409(self):
        self.declenche.side_effect = serve.blink_engine.BusyError("occupe")
        self.appeler(f"camera=Jardin&token={self.jeton}")
        self.assertEqual(self.handler.send_json.call_args.args[1], 409)

    def test_ne_depend_pas_de_hote_autorise_ni_jeton_session(self):
        # Le point du webhook : un appelant qui echouerait les deux controles
        # de session doit quand meme reussir avec le bon secret.
        self.handler.hote_autorise = mock.Mock(return_value=False)
        self.handler.jeton_valide = mock.Mock(return_value=False)
        (self.handler.paths["snapshots"] / "Jardin").mkdir(parents=True)
        (self.handler.paths["snapshots"] / "Jardin" / "x.jpg").write_bytes(b"photo")
        self.appeler(f"camera=Jardin&token={self.jeton}")
        self.handler.hote_autorise.assert_not_called()
        self.handler.jeton_valide.assert_not_called()
        self.handler.send_error.assert_not_called()

    def test_do_get_route_vers_le_webhook_avant_hote_autorise(self):
        self.handler.path = f"{serve.WEBHOOK_SNAPSHOT_ROUTE}?camera=Jardin&token={self.jeton}"
        self.handler.hote_autorise = mock.Mock(return_value=False)
        self.handler.gerer_webhook_snapshot = mock.Mock()
        serve.Handler.do_GET(self.handler)
        self.handler.gerer_webhook_snapshot.assert_called_once()
        self.handler.hote_autorise.assert_not_called()


class SupprimerSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.handler = serve.Handler.__new__(serve.Handler)
        self.racine = Path(tempfile.mkdtemp(prefix="blink-supprimer-snapshot-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.racine, ignore_errors=True))
        self.handler.paths = {"snapshots": self.racine}
        (self.racine / "Jardin").mkdir()
        self.fichier = self.racine / "Jardin" / "2026-09-14_10-00-00Z_abcd1234.jpg"
        self.fichier.write_bytes(b"photo")
        self.handler.hote_autorise = lambda: True
        self.handler.jeton_valide = lambda: True
        self.handler.send_json = mock.Mock()
        self.handler.send_error = mock.Mock()

    def poster(self, fichier: str):
        corps = json.dumps({"fichier": fichier}).encode()
        self.handler.path = "/api/snapshot-supprimer"
        self.handler.headers = {"Content-Length": str(len(corps))}
        self.handler.rfile = io.BytesIO(corps)
        self.handler.do_POST()

    def test_supprime_le_fichier_designe(self):
        self.poster("Jardin/2026-09-14_10-00-00Z_abcd1234.jpg")
        self.assertFalse(self.fichier.exists())
        self.handler.send_json.assert_called_once_with({"ok": True})

    def test_traversee_de_chemin_refusee_en_404(self):
        self.poster("../../../../etc/passwd")
        self.assertTrue(self.fichier.exists(), "le fichier legitime ne doit pas bouger")
        self.handler.send_error.assert_called_once_with(404)


if __name__ == "__main__":
    unittest.main()
