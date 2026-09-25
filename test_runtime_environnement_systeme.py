"""Issue #23 : les programmes du système lancés depuis le bundle Linux
(systemctl, xdg-open...) ne doivent pas hériter du LD_LIBRARY_PATH que le
lanceur de PyInstaller préfixe du dossier _internal.

tests.py le vérifie aussi de bout en bout sur le bundle Linux, avec un faux
systemctl qui note ce qu'il reçoit."""

import os
import sys
import unittest
from unittest import mock

import runtime

BUNDLE = "/opt/blink2video/_internal"


class RetablirEnvironnementSysteme(unittest.TestCase):
    def appliquer(self, environ: dict, fige: bool = True,
                  plateforme: str = "linux") -> dict:
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(runtime, "frozen", return_value=fige), \
                mock.patch.object(sys, "platform", plateforme):
            runtime.retablir_environnement_systeme()
            return dict(os.environ)

    def test_valeur_d_origine_rendue(self):
        env = self.appliquer({"LD_LIBRARY_PATH": f"{BUNDLE}:/usr/local/lib",
                              "LD_LIBRARY_PATH_ORIG": "/usr/local/lib"})
        self.assertEqual(env["LD_LIBRARY_PATH"], "/usr/local/lib")

    def test_variable_absente_avant_le_lanceur_retiree(self):
        env = self.appliquer({"LD_LIBRARY_PATH": BUNDLE, "PATH": "/usr/bin"})
        self.assertNotIn("LD_LIBRARY_PATH", env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_sources_intactes(self):
        # Hors bundle, personne n'a touché à la variable : c'est celle de
        # l'utilisateur, à transmettre telle quelle.
        env = self.appliquer({"LD_LIBRARY_PATH": "/choix/utilisateur"}, fige=False)
        self.assertEqual(env["LD_LIBRARY_PATH"], "/choix/utilisateur")

    def test_windows_et_macos_intacts(self):
        for plateforme in ("win32", "darwin"):
            with self.subTest(plateforme=plateforme):
                env = self.appliquer({"LD_LIBRARY_PATH": BUNDLE,
                                      "LD_LIBRARY_PATH_ORIG": "/usr/lib"},
                                     plateforme=plateforme)
                self.assertEqual(env["LD_LIBRARY_PATH"], BUNDLE)

    def test_deux_appels_meme_resultat(self):
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": BUNDLE,
                                          "LD_LIBRARY_PATH_ORIG": "/usr/lib"},
                             clear=True), \
                mock.patch.object(runtime, "frozen", return_value=True), \
                mock.patch.object(sys, "platform", "linux"):
            runtime.retablir_environnement_systeme()
            runtime.retablir_environnement_systeme()
            self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/usr/lib")


if __name__ == "__main__":
    unittest.main()
