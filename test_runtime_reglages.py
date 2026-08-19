"""Non-régression de runtime.lire_reglages / ecrire_reglages / standard
(AUDIT-2026-08-13.md, section 28.32/28.34/28.36/28.37/28.39/28.56) :
cadences USB/cloud, port, horodatage, fuseau, activation jour/semaine/mois
du merge et activation du téléchargement automatique, réglables depuis la
page web plutôt que figés dans la constante STANDARD.

Anciennement test_runtime_cadences.py : renommé avec les fonctions, le
port, l'horodatage, le fuseau puis les coches de merge ayant rejoint les
cadences dans le même fichier de réglages."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import runtime


class TestsReglages(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_reglages_")
        self.dossier = Path(self.temporaire.name)
        self.patch = mock.patch.object(runtime, "app_dir", return_value=self.dossier)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temporaire.cleanup()

    def test_fichier_absent_rend_les_valeurs_par_defaut(self):
        self.assertEqual(runtime.lire_reglages(), dict(runtime.REGLAGES_DEFAUT))

    def test_fichier_illisible_rend_les_valeurs_par_defaut(self):
        (self.dossier / runtime.REGLAGES).write_text("{pas du json", encoding="utf-8")
        self.assertEqual(runtime.lire_reglages(), dict(runtime.REGLAGES_DEFAUT))

    def test_ecrire_puis_lire_conserve_les_valeurs(self):
        runtime.ecrire_reglages(usb_minutes=7, cloud_minutes=2, port=8899, timestamp=False,
                                timezone="America/New_York", merge_jour=True,
                                merge_semaine=False, merge_mois=False, download_auto=False)
        self.assertEqual(
            runtime.lire_reglages(),
            {"usb_minutes": 7, "cloud_minutes": 2, "port": 8899, "timestamp": False,
             "timezone": "America/New_York", "merge_jour": True, "merge_semaine": False,
             "merge_mois": False, "download_auto": False})

    def test_valeurs_partielles_completees_par_les_defauts(self):
        (self.dossier / runtime.REGLAGES).write_text(
            '{"usb_minutes": 15}', encoding="utf-8")
        self.assertEqual(
            runtime.lire_reglages(),
            {"usb_minutes": 15,
             "cloud_minutes": runtime.REGLAGES_DEFAUT["cloud_minutes"],
             "port": runtime.REGLAGES_DEFAUT["port"],
             "timestamp": runtime.REGLAGES_DEFAUT["timestamp"],
             "timezone": runtime.REGLAGES_DEFAUT["timezone"],
             "merge_jour": runtime.REGLAGES_DEFAUT["merge_jour"],
             "merge_semaine": runtime.REGLAGES_DEFAUT["merge_semaine"],
             "merge_mois": runtime.REGLAGES_DEFAUT["merge_mois"],
             "download_auto": runtime.REGLAGES_DEFAUT["download_auto"]})

    def test_fuseau_vide_dans_le_fichier_retombe_sur_le_defaut(self):
        (self.dossier / runtime.REGLAGES).write_text(
            '{"timezone": ""}', encoding="utf-8")
        self.assertEqual(runtime.lire_reglages()["timezone"], runtime.REGLAGES_DEFAUT["timezone"])

    def test_standard_reprend_les_verbes_historiques_avec_reglages_par_defaut(self):
        self.assertEqual(
            runtime.standard(),
            ("serve", "--port", "8765", "--timezone", "Europe/Paris",
             "watch", "--loop", "10",
             "download", "--from", "usb", "--loop", "10",
             "download", "--from", "cloud", "--loop", "1",
             "merge", "--loop", "5", "--timezone", "Europe/Paris"))

    def test_standard_reflete_les_reglages_enregistres(self):
        runtime.ecrire_reglages(usb_minutes=20, cloud_minutes=3, port=9090, timestamp=True,
                                timezone="Asia/Tokyo", merge_jour=True, merge_semaine=True,
                                merge_mois=True, download_auto=True)
        composition = runtime.standard()
        self.assertEqual(
            composition,
            ("serve", "--port", "9090", "--timezone", "Asia/Tokyo",
             "watch", "--loop", "10",
             "download", "--from", "usb", "--loop", "20",
             "download", "--from", "cloud", "--loop", "3",
             "merge", "--loop", "5", "--timezone", "Asia/Tokyo"))

    def test_standard_ajoute_no_timestamp_quand_desactive(self):
        runtime.ecrire_reglages(usb_minutes=10, cloud_minutes=1, port=8765, timestamp=False,
                                timezone="Europe/Paris", merge_jour=True, merge_semaine=True,
                                merge_mois=True, download_auto=True)
        composition = runtime.standard()
        self.assertEqual(composition[-6:],
                         ("merge", "--loop", "5", "--timezone", "Europe/Paris",
                          "--no-timestamp"))

    def test_standard_omet_merge_quand_journaliere_desactivee(self):
        # Semaine et mois derivent des videos journalieres : les couper
        # toutes les trois revient a ne plus lancer merge du tout, plutot
        # que de le lancer pour rien.
        runtime.ecrire_reglages(usb_minutes=10, cloud_minutes=1, port=8765, timestamp=True,
                                timezone="Europe/Paris", merge_jour=False, merge_semaine=True,
                                merge_mois=True, download_auto=True)
        composition = runtime.standard()
        self.assertNotIn("merge", composition)

    def test_standard_omet_download_quand_desactive(self):
        # Coche pensee pour qui ne veut que le direct (voir BACKLOG.md) :
        # aucun des deux "download" (usb, cloud) ne doit rester, watch/serve/
        # merge continuent de tourner normalement.
        runtime.ecrire_reglages(usb_minutes=10, cloud_minutes=1, port=8765, timestamp=True,
                                timezone="Europe/Paris", merge_jour=True, merge_semaine=True,
                                merge_mois=True, download_auto=False)
        composition = runtime.standard()
        self.assertNotIn("download", composition)
        self.assertIn("serve", composition)
        self.assertIn("watch", composition)
        self.assertIn("merge", composition)

    def test_standard_ajoute_no_weekly_et_no_monthly_selon_les_reglages(self):
        runtime.ecrire_reglages(usb_minutes=10, cloud_minutes=1, port=8765, timestamp=True,
                                timezone="Europe/Paris", merge_jour=True, merge_semaine=False,
                                merge_mois=False, download_auto=True)
        composition = runtime.standard()
        self.assertEqual(composition[-7:],
                         ("merge", "--loop", "5", "--timezone", "Europe/Paris",
                          "--no-weekly", "--no-monthly"))

    def test_les_elements_du_bloc_serve_sont_fixes(self):
        # blink_cli.py greffe le supplément de « start » juste après ce bloc
        # (composition[:LONGUEUR_BLOC_SERVE]) : un --port ou --timezone tapé
        # à la main arrive donc après celui-ci et l'emporte (argparse
        # retient la dernière occurrence).
        composition = runtime.standard()
        n = runtime.LONGUEUR_BLOC_SERVE
        self.assertEqual(composition[:n],
                         ("serve", "--port", "8765", "--timezone", "Europe/Paris"))
        self.assertEqual(composition[n], "watch")

    def test_un_port_explicite_dans_le_supplement_arrive_apres_le_defaut(self):
        composition = runtime.standard()
        n = runtime.LONGUEUR_BLOC_SERVE
        supplement = ["--port", "9999"]
        assemblee = [*composition[:n], *supplement, *composition[n:]]
        indice_defaut = assemblee.index("8765")
        indice_explicite = assemblee.index("9999")
        self.assertLess(indice_defaut, indice_explicite,
                         "le port explicite doit venir apres le defaut pour l'emporter")


if __name__ == "__main__":
    unittest.main()
