"""Garde-fous de l'édition Windows 7, sans réseau ni construction du bundle."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import build
import build_blinkpy_win7
import maj
import runtime


class Windows7BuildTests(unittest.TestCase):
    def test_build_profiles_are_fully_isolated(self):
        normal = set(build._chemins(False))
        legacy = set(build._chemins(True))
        self.assertTrue(normal.isdisjoint(legacy))
        self.assertEqual(build.WIN7_PYTHON, (3, 8, 10))
        self.assertEqual(build_blinkpy_win7.WIN7_VERSION, "0.25.9+win7.1")

    def test_win7_build_rejects_another_platform(self):
        with mock.patch.object(build.sys, "platform", "linux"):
            with self.assertRaises(SystemExit) as erreur:
                build.verifier_interpreteur_win7()
        self.assertIn("Windows", str(erreur.exception))

    def test_marker_identifies_only_frozen_bundle(self):
        with tempfile.TemporaryDirectory() as dossier:
            racine = Path(dossier)
            (racine / runtime.WINDOWS7_BUILD_MARKER).write_text("win7")
            with mock.patch.object(runtime, "resource_dir", return_value=racine):
                with mock.patch.object(runtime, "frozen", return_value=False):
                    self.assertFalse(runtime.build_windows7())
                with mock.patch.object(runtime, "frozen", return_value=True):
                    self.assertTrue(runtime.build_windows7())
                    self.assertIn("Windows 7", runtime.version_affichee())

    def test_update_is_disabled_without_network(self):
        with mock.patch.object(runtime, "build_windows7", return_value=True), \
                mock.patch.object(maj, "_interroger") as interroger:
            self.assertEqual(maj.disponible(force=True), {})
            with redirect_stdout(StringIO()) as sortie:
                self.assertEqual(maj.installer(force=True), 0)
            self.assertIn("désactivée", sortie.getvalue())
            interroger.assert_not_called()

    def test_normal_windows_never_selects_legacy_asset(self):
        assets = [
            {"name": "blink2video-windows7-x86_64-experimental.zip",
             "browser_download_url": "legacy", "size": 1},
            {"name": "blink2video-windows-x86_64.zip",
             "browser_download_url": "normal", "size": 2},
        ]
        with mock.patch.object(maj.sys, "platform", "win32"), \
                mock.patch.object(maj.platform, "machine", return_value="AMD64"):
            choisie = maj._archive_de_ce_systeme(assets)
        self.assertEqual(choisie.get("url"), "normal")


if __name__ == "__main__":
    unittest.main()
