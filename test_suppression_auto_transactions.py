"""Les autorisations de suppression ne doivent jamais revenir silencieusement.

Requêtes simultanées et changements de stockage, sans caméra ni processus réel.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-suppression-import-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import runtime
import serve


class TestSuppressionAutoTransactions(unittest.TestCase):
    def setUp(self):
        temporaire = tempfile.TemporaryDirectory(prefix="blink-suppression-transaction-")
        self.addCleanup(temporaire.cleanup)
        self.ancre = Path(temporaire.name).resolve()
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch.dict(os.environ))
        os.environ.pop("BLINK_HOME", None)
        self.patches.enter_context(
            mock.patch.object(runtime, "_dossier_ancre", return_value=self.ancre))
        # Jamais le vrai dossier d'état ni le vrai dossier Documents.
        self.patches.enter_context(
            mock.patch.object(runtime, "_dossier_etat_standard", return_value=self.ancre))
        self.patches.enter_context(mock.patch.object(runtime, "_ETATS_CREES", set()))
        self.patches.enter_context(mock.patch(
            "platformdirs.user_documents_dir", return_value=str(self.ancre / "Documents")))
        self.patches.enter_context(
            mock.patch.object(runtime, "_dossier_controle", return_value=self.ancre))
        self.patches.enter_context(
            mock.patch.object(runtime, "identite_processus", return_value="processus-test"))
        self.patches.enter_context(
            mock.patch.object(runtime, "processus_vivant", return_value=True))
        self.entries = {
            "salon": {"camera": "Salon", "network_id": "111", "device_id": "1"},
            "jardin": {"camera": "Jardin", "network_id": "111", "device_id": "2"},
        }
        self.cle_salon = serve.blink_registre.camera_setting_key_from_entry(
            self.entries["salon"])
        self.cle_jardin = serve.blink_registre.camera_setting_key_from_entry(
            self.entries["jardin"])
        self.patches.enter_context(
            mock.patch.object(serve, "read_entries", return_value=self.entries))
        self.patches.enter_context(mock.patch.object(
            serve.md, "load_json", return_value={"cameras": {"Salon": {}, "Jardin": {}}}))

    def poster(self, camera, actif):
        handler = serve.Handler.__new__(serve.Handler)
        handler.path = "/api/suppression-auto"
        handler.paths = {"input": self.ancre / "clips"}
        corps = json.dumps({"camera": camera, "actif": actif}).encode("utf-8")
        handler.headers = {
            "Content-Length": str(len(corps)), "Host": "127.0.0.1",
            "X-Blink-Token": serve.TOKEN,
        }
        handler.rfile = io.BytesIO(corps)
        reponses = []
        handler.send_json = lambda payload, code=200: reponses.append((code, payload))
        handler.do_POST()
        self.assertEqual(len(reponses), 1)
        return reponses[0]

    def test_deux_requetes_ne_perdent_pas_la_desactivation(self):
        runtime.ecrire_suppression_auto({self.cle_salon})
        lire = runtime.lire_suppression_auto
        ecrire = runtime.ecrire_suppression_auto
        verrou_configuration = runtime.verrou_configuration
        premiere_lecture = threading.Event()
        seconde_tentative = threading.Event()
        premiere_ecriture = threading.Event()
        reponses, erreurs = [], []

        @contextlib.contextmanager
        def reserver(*args, **kwargs):
            if threading.current_thread().name == "activation":
                seconde_tentative.set()
            with verrou_configuration(*args, **kwargs):
                yield

        def lire_ordonne():
            cameras = lire()
            if threading.current_thread().name == "desactivation":
                premiere_lecture.set()
                if not seconde_tentative.wait(5):
                    raise AssertionError("La seconde requête n'a pas démarré.")
            else:
                # Sans verrou, les deux lectures précèdent la première écriture.
                seconde_tentative.set()
            return cameras

        def ecrire_ordonne(cameras):
            if threading.current_thread().name == "activation":
                if not premiere_ecriture.wait(5):
                    raise AssertionError("La désactivation n'a pas été enregistrée.")
            ecrire(cameras)
            if threading.current_thread().name == "desactivation":
                premiere_ecriture.set()

        def requete(camera, actif):
            try:
                reponses.append(self.poster(camera, actif))
            except Exception as erreur:
                erreurs.append(erreur)

        with mock.patch.object(runtime, "verrou_configuration", side_effect=reserver) as verrou, \
                mock.patch.object(runtime, "lire_suppression_auto", side_effect=lire_ordonne), \
                mock.patch.object(runtime, "ecrire_suppression_auto", side_effect=ecrire_ordonne):
            premier = threading.Thread(
                target=requete, args=("Salon", False), name="desactivation", daemon=True)
            second = threading.Thread(
                target=requete, args=("Jardin", True), name="activation", daemon=True)
            premier.start()
            try:
                self.assertTrue(premiere_lecture.wait(5))
                second.start()
                premier.join(8)
                second.join(8)
                self.assertFalse(premier.is_alive())
                self.assertFalse(second.is_alive())
            finally:
                seconde_tentative.set()
                premiere_ecriture.set()
                premier.join(6)
                if second.ident is not None:
                    second.join(6)
            self.assertEqual(erreurs, [])
            self.assertEqual(sorted(code for code, _ in reponses), [200, 200])
            self.assertEqual(lire(), {self.cle_jardin})
            self.assertEqual(verrou.call_count, 2)
            for appel in verrou.call_args_list:
                owner = appel.args[0] if appel.args else appel.kwargs.get("owner")
                self.assertEqual(owner, "suppression-auto")
                self.assertEqual(appel.kwargs.get("attente"), 5)

    def test_verrou_occupe_repond_409_sans_ecriture(self):
        runtime.ecrire_suppression_auto({self.cle_salon})
        with mock.patch.object(runtime, "verrou_configuration",
                               side_effect=runtime.BusyError("configuration occupée")), \
                mock.patch.object(runtime, "ecrire_suppression_auto") as ecrire:
            code, reponse = self.poster("Salon", False)
        self.assertEqual(code, 409)
        self.assertIn("error", reponse)
        ecrire.assert_not_called()
        self.assertEqual(runtime.lire_suppression_auto(), {self.cle_salon})

    def test_echec_ecriture_repond_500_et_libere_le_verrou(self):
        runtime.ecrire_suppression_auto({self.cle_salon})
        with mock.patch.object(runtime, "ecrire_suppression_auto",
                               side_effect=OSError("disque inaccessible")):
            code, reponse = self.poster("Salon", False)
        self.assertEqual(code, 500)
        self.assertIn("error", reponse)
        self.assertEqual(runtime.lire_suppression_auto(), {self.cle_salon})
        with runtime.verrou_configuration(owner="verification", attente=0):
            pass

    def test_changer_les_sorties_conserve_langue_et_desactivation(self):
        """Depuis 0.14, les préférences restent dans le dossier d'état : un
        aller-retour du dossier des sorties ne les copie ni ne les perd."""
        runtime.ecrire_suppression_auto({self.cle_salon})
        runtime.ecrire_langue("en")
        autre = self.ancre / "autre"
        runtime.ecrire_dossier_stockage(str(autre))
        self.assertEqual(runtime.app_dir(), self.ancre)
        self.assertEqual(runtime.dossier_sorties(), autre)
        self.assertEqual(runtime.lire_suppression_auto(), {self.cle_salon})
        self.assertEqual(runtime.lire_langue(), "en")

        runtime.ecrire_suppression_auto(set())
        runtime.ecrire_langue("fr")
        runtime.ecrire_dossier_stockage("")
        self.assertEqual(runtime.lire_suppression_auto(), set())
        self.assertEqual(runtime.lire_langue(), "fr")
        self.assertFalse((autre / runtime.SUPPRESSION_AUTO).exists())

    def test_preferences_d_un_dossier_de_sorties_jamais_adoptees(self):
        # D'anciennes autorisations de suppression laissées dans un dossier
        # (celui d'une version ≤ 0.13, par exemple) ne s'appliquent pas
        # parce qu'il devient celui des sorties.
        autre = self.ancre / "autre"
        autre.mkdir()
        (autre / runtime.SUPPRESSION_AUTO).write_text(
            json.dumps([self.cle_salon]), encoding="utf-8")
        (autre / runtime.LANGUE).write_text("en", encoding="utf-8")
        runtime.ecrire_dossier_stockage(str(autre))
        self.assertEqual(runtime.lire_suppression_auto(), set())
        self.assertEqual(runtime.lire_langue(), "fr")

    def test_echec_d_enregistrement_ne_change_ni_sorties_ni_preferences(self):
        runtime.ecrire_suppression_auto({self.cle_salon})
        runtime.ecrire_langue("en")
        avant = runtime.dossier_sorties()
        with mock.patch.object(runtime, "ecrire_reglages",
                               side_effect=OSError("réglages refusés")):
            with self.assertRaisesRegex(OSError, "réglages refusés"):
                runtime.ecrire_dossier_stockage(str(self.ancre / "autre"))
        self.assertEqual(runtime.dossier_sorties(), avant)
        self.assertEqual(runtime.lire_suppression_auto(), {self.cle_salon})
        self.assertEqual(runtime.lire_langue(), "en")


if __name__ == "__main__":
    unittest.main()
