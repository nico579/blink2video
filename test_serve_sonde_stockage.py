"""Vérifie la sonde de stockage avec de vrais fichiers, tous temporaires."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock


_IMPORT_HOME = tempfile.TemporaryDirectory(prefix="blink-sonde-stockage-import-")
with mock.patch.dict(os.environ, {"BLINK_BOOTSTRAP": "none",
                                  "BLINK_HOME": _IMPORT_HOME.name}):
    import serve


class TestsSondeStockage(unittest.TestCase):
    def setUp(self):
        temporaire = tempfile.TemporaryDirectory(prefix="blink-sonde-stockage-")
        self.addCleanup(temporaire.cleanup)
        self.dossier = Path(temporaire.name)
        # Conserver la vraie fabrique avant de décorer celle utilisée par serve.
        self.creer_sonde = tempfile.NamedTemporaryFile

    def test_fichier_homonyme_et_autres_fichiers_restent_intacts(self):
        contenus = {
            ".blink_ecriture_test": b"contenu utilisateur a conserver\x00\xff",
            ".blink_ecriture_test-autre": b"autre sonde preexistante",
            "video.mp4": b"video utilisateur",
        }
        for nom, contenu in contenus.items():
            (self.dossier / nom).write_bytes(contenu)

        serve._verifier_dossier_reglages(str(self.dossier))

        self.assertEqual({p.name for p in self.dossier.iterdir()}, set(contenus))
        for nom, contenu in contenus.items():
            self.assertEqual((self.dossier / nom).read_bytes(), contenu)

    def test_dossier_absent_est_cree_sans_residu(self):
        dossier = self.dossier / "nouveau" / "archives"
        serve._verifier_dossier_reglages(str(dossier))
        self.assertTrue(dossier.is_dir())
        self.assertEqual(list(dossier.iterdir()), [])

    def test_sondes_successives_sont_uniques_et_nettoyees(self):
        chemins = []
        ecritures = []

        @contextlib.contextmanager
        def observer(*args, **kwargs):
            with self.creer_sonde(*args, **kwargs) as sonde:
                chemin = Path(sonde.name)
                chemins.append(chemin)
                self.assertTrue(chemin.is_file())
                self.assertEqual(chemin.parent, self.dossier)
                self.assertTrue(chemin.name.startswith(".blink_ecriture_test-"))
                yield sonde
                ecritures.append(sonde.tell())

        with mock.patch.object(tempfile, "NamedTemporaryFile", side_effect=observer):
            for _ in range(3):
                serve._verifier_dossier_reglages(str(self.dossier))
                self.assertEqual(list(self.dossier.iterdir()), [])

        self.assertEqual(len(set(chemins)), 3)
        self.assertTrue(all(taille > 0 for taille in ecritures))
        self.assertTrue(all(not chemin.exists() for chemin in chemins))

    def test_sondes_concurrentes_coexistent_sans_conflit(self):
        chemins = []
        presents_ensemble = []
        verrou = threading.Lock()

        def constater_chevauchement():
            presents_ensemble.extend(chemin.is_file() for chemin in chemins)

        rendez_vous = threading.Barrier(
            3, action=constater_chevauchement, timeout=5)

        @contextlib.contextmanager
        def faire_chevaucher(*args, **kwargs):
            with self.creer_sonde(*args, **kwargs) as sonde:
                with verrou:
                    chemins.append(Path(sonde.name))
                rendez_vous.wait()
                yield sonde

        with mock.patch.object(tempfile, "NamedTemporaryFile",
                               side_effect=faire_chevaucher):
            with ThreadPoolExecutor(max_workers=3) as executeur:
                travaux = [executeur.submit(
                    serve._verifier_dossier_reglages, str(self.dossier))
                    for _ in range(3)]
                for travail in travaux:
                    travail.result(timeout=10)

        self.assertEqual(len(set(chemins)), 3)
        self.assertEqual(presents_ensemble, [True, True, True])
        self.assertEqual(list(self.dossier.iterdir()), [])

    def test_echec_ecriture_ou_flush_nettoie_la_sonde_reelle(self):
        temoin = self.dossier / ".blink_ecriture_test"
        temoin.write_bytes(b"a conserver")

        for operation in ("write", "flush"):
            with self.subTest(operation=operation):
                chemins = []
                erreur = OSError(operation + " refuse")

                @contextlib.contextmanager
                def echouer(*args, **kwargs):
                    with self.creer_sonde(*args, **kwargs) as sonde:
                        chemins.append(Path(sonde.name))
                        espion = mock.Mock(wraps=sonde)
                        getattr(espion, operation).side_effect = erreur
                        yield espion

                with mock.patch.object(tempfile, "NamedTemporaryFile", side_effect=echouer):
                    with self.assertRaises(serve._ReglagesInvalides) as capture:
                        serve._verifier_dossier_reglages(str(self.dossier))

                self.assertIs(capture.exception.__cause__, erreur)
                self.assertEqual(str(capture.exception),
                                 "Dossier de stockage inaccessible : " + str(erreur))
                self.assertEqual(len(chemins), 1)
                self.assertFalse(chemins[0].exists())
                self.assertEqual(list(self.dossier.iterdir()), [temoin])
                self.assertEqual(temoin.read_bytes(), b"a conserver")

    def test_echec_creation_sonde_est_un_refus_sans_residu(self):
        erreur = OSError("creation refusee")
        with mock.patch.object(tempfile, "NamedTemporaryFile", side_effect=erreur):
            with self.assertRaises(serve._ReglagesInvalides) as capture:
                serve._verifier_dossier_reglages(str(self.dossier))
        self.assertIs(capture.exception.__cause__, erreur)
        self.assertEqual(list(self.dossier.iterdir()), [])

    def test_http_refuse_un_fichier_comme_dossier_avant_enregistrement(self):
        fichier = self.dossier / "archives"
        fichier.write_bytes(b"contenu utilisateur")
        payload = {"usb_minutes": 5, "cloud_minutes": 10, "port": 8765,
                   "timezone": "UTC", "storage_dir": str(fichier)}
        corps = json.dumps(payload).encode("utf-8")
        handler = object.__new__(serve.Handler)
        handler.path = "/api/reglages"
        handler.paths = {}
        handler.initial_setup = False
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {
            "Host": "127.0.0.1:8765", "X-Blink-Token": serve.TOKEN,
            "Content-Length": str(len(corps)),
        }
        handler.rfile = io.BytesIO(corps)
        handler.send_json = mock.Mock()
        handler.send_error = mock.Mock()
        handler.repondre_puis_redemarrer = mock.Mock()

        with mock.patch.object(serve.runtime, "ecrire_dossier_stockage") as stockage, \
                mock.patch.object(serve.runtime, "verrou_configuration") as verrou:
            handler.do_POST()

        handler.send_json.assert_called_once()
        reponse, statut = handler.send_json.call_args.args
        self.assertEqual(statut, 400)
        self.assertTrue(reponse["error"].startswith("Dossier de stockage inaccessible : "))
        handler.send_error.assert_not_called()
        stockage.assert_not_called()
        verrou.assert_not_called()
        handler.repondre_puis_redemarrer.assert_not_called()
        self.assertEqual(fichier.read_bytes(), b"contenu utilisateur")


if __name__ == "__main__":
    unittest.main()
