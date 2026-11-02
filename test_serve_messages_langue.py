"""Les messages du serveur suivent la langue de la page (issue #121)."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-messages-langue-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import runtime  # noqa: E402
import serve  # noqa: E402 - environnement isole avant import


class ModuleOccupe(unittest.TestCase):
    """Le message qu'a vu Markus. Seul son texte suit la langue : le statut
    HTTP 409 d'un module occupé ne change pas, c'est lui que les scripts
    doivent tester."""

    def setUp(self):
        serve.MODULE_SLOT_INFO.clear()
        self.addCleanup(serve.MODULE_SLOT_INFO.clear)

    def _message(self, langue: str) -> str:
        with mock.patch.object(runtime, "lire_langue", return_value=langue):
            return serve._slot_occupe_message()

    def test_occupation_inconnue(self):
        self.assertEqual(self._message("en"), "The module is already busy (live view or refresh).")
        self.assertIn("déjà occupé", self._message("fr"))

    def test_occupation_traduite_identifiant_interne_intact(self):
        serve._slot_pris("direct MSE", "Jardin")
        serve.MODULE_SLOT_INFO["depuis"] = time.monotonic() - 12
        anglais = self._message("en")
        self.assertTrue(anglais.startswith("The module is already busy: live view (Jardin), for 1"),
                        anglais)
        self.assertIn("direct (Jardin)", self._message("fr"))
        # L'identifiant interne, comparé tel quel ailleurs, n'est pas traduit.
        self.assertEqual(serve.MODULE_SLOT_INFO["quoi"], "direct MSE")


class TitresDeSection(unittest.TestCase):
    """L'enfant download écrit ses titres dans sa langue ; la page doit
    recevoir la même clé de traduction dans les deux cas."""

    def test_cloud_dans_les_deux_langues(self):
        self.assertIn("CLOUD DE L'ABONNEMENT", serve._TITRES_CLOUD)
        self.assertIn("SUBSCRIPTION CLOUD", serve._TITRES_CLOUD)

    def test_stockage_local_dans_les_deux_langues(self):
        self.assertEqual(serve._hub_section_usb("STOCKAGE LOCAL : Maison"), "Maison")
        self.assertEqual(serve._hub_section_usb("LOCAL STORAGE: Maison"), "Maison")
        self.assertIsNone(serve._hub_section_usb("SUBSCRIPTION CLOUD"))


if __name__ == "__main__":
    unittest.main()
