"""Non-régression de runtime.lire_cadences / ecrire_cadences / standard
(AUDIT-2026-08-13.md, section 28.32) : cadences USB/cloud réglables depuis
la page web plutôt que figées dans la constante STANDARD."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import runtime


class TestsCadences(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_cadences_")
        self.dossier = Path(self.temporaire.name)
        self.patch = mock.patch.object(runtime, "app_dir", return_value=self.dossier)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temporaire.cleanup()

    def test_fichier_absent_rend_les_valeurs_par_defaut(self):
        self.assertEqual(runtime.lire_cadences(), dict(runtime.CADENCES_DEFAUT))

    def test_fichier_illisible_rend_les_valeurs_par_defaut(self):
        (self.dossier / runtime.REGLAGES).write_text("{pas du json", encoding="utf-8")
        self.assertEqual(runtime.lire_cadences(), dict(runtime.CADENCES_DEFAUT))

    def test_ecrire_puis_lire_conserve_les_valeurs(self):
        runtime.ecrire_cadences(usb_minutes=7, cloud_minutes=2)
        self.assertEqual(
            runtime.lire_cadences(), {"usb_minutes": 7, "cloud_minutes": 2})

    def test_valeurs_partielles_completees_par_les_defauts(self):
        (self.dossier / runtime.REGLAGES).write_text(
            '{"usb_minutes": 15}', encoding="utf-8")
        self.assertEqual(
            runtime.lire_cadences(),
            {"usb_minutes": 15, "cloud_minutes": runtime.CADENCES_DEFAUT["cloud_minutes"]})

    def test_standard_reprend_les_verbes_historiques_avec_cadences_par_defaut(self):
        self.assertEqual(
            runtime.standard(),
            ("serve",
             "watch", "--loop", "10",
             "download", "--from", "usb", "--loop", "10",
             "download", "--from", "cloud", "--loop", "1",
             "merge", "--loop", "5"))

    def test_standard_reflete_les_cadences_reglees(self):
        runtime.ecrire_cadences(usb_minutes=20, cloud_minutes=3)
        composition = runtime.standard()
        self.assertIn("20", composition)
        self.assertIn("3", composition)
        self.assertEqual(
            composition,
            ("serve",
             "watch", "--loop", "10",
             "download", "--from", "usb", "--loop", "20",
             "download", "--from", "cloud", "--loop", "3",
             "merge", "--loop", "5"))


if __name__ == "__main__":
    unittest.main()
