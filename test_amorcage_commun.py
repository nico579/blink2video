"""Le moteur d'amorçage de blink2video est celui des quatre applications.

``_amorcage.py`` est une copie octet pour octet de ``nico579_commons.amorcage`` :
il tourne avant l'installation de la bibliothèque commune, qu'il ne peut donc
pas importer. Ces tests vérifient que la copie n'a pas dérivé du paquet
installé et que runtime.bootstrap() l'appelle comme il faut. Le moteur lui-même
(modes, venv, pip, relance) est testé dans nico579-commons (tests/test_amorcage.py).
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import runtime

RACINE = Path(__file__).resolve().parent


def _lignes(chemin):
    """Le texte sans égard aux fins de ligne : un dépôt récupéré sous Windows
    peut les convertir, sans que la copie ait dérivé."""
    return Path(chemin).read_bytes().decode("utf-8").splitlines()


class CopieDuCommun(unittest.TestCase):
    def test_la_copie_est_identique_au_module_du_paquet(self):
        from nico579_commons import amorcage
        self.assertEqual(
            _lignes(RACINE / "_amorcage.py"), _lignes(amorcage.__file__),
            "_amorcage.py a dérivé de nico579_commons.amorcage : recopier le "
            "fichier du paquet (et monter l'épingle de nico579-commons).")


class Branchement(unittest.TestCase):
    def test_la_configuration_de_blink2video(self):
        moteur = runtime.amorcage()
        self.assertEqual(moteur.nom, "blink2video")
        self.assertEqual(moteur.variable, "BLINK_BOOTSTRAP")
        self.assertEqual(moteur.venv, Path.home() / ".blink2video" / "venv")
        self.assertTrue(moteur.reutiliser_environnement)  # l'image Docker : rien à créer

    def test_la_langue_est_celle_de_la_page(self):
        with mock.patch.object(runtime, "lire_langue", return_value="en"):
            self.assertIn("Creating", runtime.amorcage().texte("creation_venv", venv="v"))
        with mock.patch.object(runtime, "lire_langue", return_value="fr"):
            self.assertIn("Création", runtime.amorcage().texte("creation_venv", venv="v"))

    def test_un_bundle_n_amorce_rien(self):
        with mock.patch.object(runtime, "frozen", return_value=True), \
                mock.patch.object(runtime, "amorcage") as amorcage:
            runtime.bootstrap()
        amorcage.assert_not_called()

    def test_l_option_est_retiree_avant_le_moteur(self):
        # --bootstrap= n'est connue d'aucun parseur de verbe : elle ne doit pas
        # atteindre argparse, et le moteur la lit dans la variable d'environnement.
        with mock.patch.object(runtime, "frozen", return_value=False), \
                mock.patch.object(runtime, "amorcage") as amorcage, \
                mock.patch.object(runtime.sys, "argv", ["blink2video", "download", "--bootstrap=none"]), \
                mock.patch.dict(runtime.os.environ, {}):
            runtime.bootstrap()
            self.assertEqual(runtime.sys.argv, ["blink2video", "download"])
            self.assertEqual(runtime.os.environ["BLINK_BOOTSTRAP"], "none")
        amorcage.return_value.lancer.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
