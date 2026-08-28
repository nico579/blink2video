"""Non-régression du dossier de stockage réglable depuis la page web
(AUDIT-2026-08-13.md, section 28.35) : app_dir() lit un petit fichier
pointeur à côté de l'exécutable plutôt que le fichier de réglages lui-même,
qui vit dans le dossier que ce pointeur désigne (sinon, boucle : il
faudrait déjà connaître le dossier pour savoir où lire le réglage qui le
donne)."""

from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock

import runtime


class TestsDossierStockage(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_ancre_")
        self.ancre = Path(self.temporaire.name)
        self.patch_ancre = mock.patch.object(runtime, "_dossier_ancre", return_value=self.ancre)
        self.patch_ancre.start()
        self.patch_env = mock.patch.dict("os.environ", {}, clear=False)
        self.patch_env.start()
        # BLINK_HOME, s'il est hérité de la session de test globale, a
        # toujours priorité : neutralisé pour isoler le pointeur.
        import os
        os.environ.pop("BLINK_HOME", None)

    def tearDown(self) -> None:
        self.patch_ancre.stop()
        self.patch_env.stop()
        self.temporaire.cleanup()

    def test_sans_pointeur_app_dir_rend_l_ancre(self):
        self.assertEqual(runtime.app_dir(), self.ancre)

    def test_pointeur_present_redirige_app_dir(self):
        cible = self.ancre / "ailleurs"
        runtime.ecrire_dossier_stockage(str(cible))
        self.assertEqual(runtime.app_dir(), cible.resolve())

    def test_pointeur_vide_efface_le_reglage(self):
        cible = self.ancre / "ailleurs"
        runtime.ecrire_dossier_stockage(str(cible))
        runtime.ecrire_dossier_stockage("")
        self.assertEqual(runtime.app_dir(), self.ancre)
        self.assertFalse((self.ancre / runtime.POINTEUR_STOCKAGE).exists())

    def test_blink_home_garde_la_priorite_sur_le_pointeur(self):
        import os
        cible_pointeur = self.ancre / "ailleurs"
        runtime.ecrire_dossier_stockage(str(cible_pointeur))
        with tempfile.TemporaryDirectory(prefix="blink_home_force_") as force:
            os.environ["BLINK_HOME"] = force
            try:
                self.assertEqual(runtime.app_dir(), Path(force).resolve())
            finally:
                del os.environ["BLINK_HOME"]

    def test_lire_dossier_stockage_reflete_app_dir(self):
        self.assertEqual(runtime.lire_dossier_stockage(), str(self.ancre))
        cible = self.ancre / "ailleurs"
        runtime.ecrire_dossier_stockage(str(cible))
        self.assertEqual(runtime.lire_dossier_stockage(), str(cible.resolve()))

    def test_app_dir_depuis_sans_pointeur_rend_l_ancre_fournie(self):
        # maj.py l'appelle avec le dossier d'installation réel, pas
        # forcément celui que _dossier_ancre() calculerait pour CE
        # processus (tournant depuis un dossier temporaire) : l'ancre est
        # donc un paramètre explicite, jamais relu depuis _dossier_ancre().
        autre_ancre = self.ancre / "installation_reelle"
        autre_ancre.mkdir()
        self.assertEqual(runtime.app_dir_depuis(autre_ancre), autre_ancre)

    def test_changement_de_dossier_copie_reglages_et_session(self):
        """Revue du 27/08 : "je perds mon authentification" en changeant de
        dossier de stockage. app_dir() démarrait vide au nouvel emplacement
        - session et réglages, écrits dans l'ancien juste avant ce même
        appel (ecrire_reglages() puis ecrire_dossier_stockage() dans
        /api/reglages), restaient orphelins."""
        (self.ancre / runtime.REGLAGES).write_text('{"port": 9999}', encoding="utf-8")
        (self.ancre / "blink_auth.json").write_text('{"token": "abc"}', encoding="utf-8")
        cible = self.ancre / "ailleurs"
        runtime.ecrire_dossier_stockage(str(cible))
        self.assertEqual(
            (cible / runtime.REGLAGES).read_text(encoding="utf-8"), '{"port": 9999}')
        self.assertEqual(
            (cible / "blink_auth.json").read_text(encoding="utf-8"), '{"token": "abc"}')
        # Copiés, pas déplacés : l'ancien emplacement reste intact.
        self.assertTrue((self.ancre / runtime.REGLAGES).exists())
        self.assertTrue((self.ancre / "blink_auth.json").exists())

    def test_changement_de_dossier_sans_session_prealable_ne_plante_pas(self):
        cible = self.ancre / "ailleurs"
        runtime.ecrire_dossier_stockage(str(cible))
        self.assertFalse((cible / "blink_auth.json").exists())

    def test_effacer_le_reglage_copie_aussi_vers_l_emplacement_par_defaut(self):
        cible = self.ancre / "ailleurs"
        runtime.ecrire_dossier_stockage(str(cible))
        (cible / "blink_auth.json").write_text('{"token": "xyz"}', encoding="utf-8")
        runtime.ecrire_dossier_stockage("")
        self.assertEqual(
            (self.ancre / "blink_auth.json").read_text(encoding="utf-8"), '{"token": "xyz"}')

    def test_app_dir_depuis_suit_le_pointeur_de_l_ancre_fournie(self):
        # Bug corrigé le 27 août 2026 (signalé sur Reddit) : maj.py forçait
        # BLINK_HOME sur le dossier d'installation lui-même pendant une mise
        # à jour, ramenant le dossier de données à celui de l'exécutable
        # même quand l'utilisateur l'avait explicitement redirigé ailleurs.
        autre_ancre = self.ancre / "installation_reelle"
        autre_ancre.mkdir()
        cible = self.ancre / "stockage_redirige"
        (autre_ancre / runtime.POINTEUR_STOCKAGE).write_text(
            str(cible), encoding="utf-8")
        self.assertEqual(runtime.app_dir_depuis(autre_ancre), cible.resolve())

    def test_changement_de_dossier_ne_perd_pas_instance_ni_demande_arret(self):
        """Les fichiers de contrôle restent à l'ancre de l'installation.

        Sinon, juste après le changement, stop cherche dans la nouvelle racine
        et ne voit plus l'instance qui tourne encore depuis l'ancienne.
        """
        dossier_instances = self.ancre / runtime.INSTANCES
        dossier_instances.mkdir()
        fiche = dossier_instances / "123.json"
        fiche.write_text(json.dumps({
            "pid": 123, "depuis": "maintenant", "verbes": [["serve"]],
            "enfants": [],
        }), encoding="utf-8")

        cible = self.ancre / "ailleurs"
        runtime.ecrire_dossier_stockage(str(cible))

        with mock.patch.object(runtime, "processus_correspond", return_value=True):
            instances = runtime.lire_instances()
        self.assertEqual([instance["pid"] for instance in instances], [123])

        runtime.demander_arret()
        self.assertTrue(runtime.arret_demande())
        self.assertTrue((self.ancre / runtime.ARRET_DEMANDE).exists())
        self.assertFalse((cible / runtime.ARRET_DEMANDE).exists())

    def test_echec_de_copie_ne_publie_pas_le_nouveau_pointeur(self):
        """La préparation est une transaction : le commit du pointeur vient dernier."""
        (self.ancre / runtime.REGLAGES).write_text('{"port": 8765}', encoding="utf-8")
        cible = self.ancre / "disque_indisponible"
        with mock.patch.object(runtime.shutil, "copy2",
                               side_effect=OSError("copie refusée")):
            with self.assertRaisesRegex(OSError, "copie refusée"):
                runtime.ecrire_dossier_stockage(str(cible))
        self.assertEqual(runtime.app_dir(), self.ancre)
        self.assertFalse((self.ancre / runtime.POINTEUR_STOCKAGE).exists())

    def test_echec_du_retour_ne_quitte_pas_la_racine_actuelle(self):
        cible = self.ancre / "ailleurs"
        runtime.ecrire_dossier_stockage(str(cible))
        (cible / "blink_auth.json").write_text('{"token": "abc"}', encoding="utf-8")
        with mock.patch.object(runtime.shutil, "copy2",
                               side_effect=OSError("ancre verrouillée")):
            with self.assertRaisesRegex(OSError, "ancre verrouillée"):
                runtime.ecrire_dossier_stockage("")
        self.assertEqual(runtime.app_dir(), cible.resolve())

    def test_ecriture_du_pointeur_ne_laisse_aucun_temporaire(self):
        runtime.ecrire_dossier_stockage(str(self.ancre / "ailleurs"))
        self.assertEqual(list(self.ancre.glob(".*blink_home*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
