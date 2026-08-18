"""Non-régression du dossier de stockage réglable depuis la page web
(AUDIT-2026-08-13.md, section 28.35) : app_dir() lit un petit fichier
pointeur à côté de l'exécutable plutôt que le fichier de réglages lui-même,
qui vit dans le dossier que ce pointeur désigne (sinon, boucle : il
faudrait déjà connaître le dossier pour savoir où lire le réglage qui le
donne)."""

from __future__ import annotations

import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
