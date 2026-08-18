"""Non-régression de autostart.est_installe (AUDIT-2026-08-13.md, section
28.30) : bascule démarrage automatique exposée depuis la page web."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import autostart


class TestsEstInstalleWindows(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_autostart_win_")
        self.dossier = Path(self.temporaire.name)

    def tearDown(self) -> None:
        self.temporaire.cleanup()

    def test_aucun_raccourci_rend_faux(self):
        with mock.patch.object(autostart, "_dossier_demarrage", return_value=self.dossier), \
             mock.patch.object(autostart.sys, "platform", "win32"):
            self.assertFalse(autostart.est_installe())

    def test_raccourci_present_rend_vrai(self):
        (self.dossier / f"{autostart.NOM}.lnk").write_text("", encoding="utf-8")
        with mock.patch.object(autostart, "_dossier_demarrage", return_value=self.dossier), \
             mock.patch.object(autostart.sys, "platform", "win32"):
            self.assertTrue(autostart.est_installe())


class TestsEstInstalleMacOS(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_autostart_mac_")
        self.domicile = Path(self.temporaire.name)
        (self.domicile / "Library" / "LaunchAgents").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporaire.cleanup()

    def test_aucun_agent_rend_faux(self):
        with mock.patch.object(autostart.Path, "home", return_value=self.domicile), \
             mock.patch.object(autostart.sys, "platform", "darwin"):
            self.assertFalse(autostart.est_installe())

    def test_agent_present_rend_vrai(self):
        cible = self.domicile / "Library" / "LaunchAgents" / f"com.nico579.{autostart.NOM}.plist"
        cible.write_text("", encoding="utf-8")
        with mock.patch.object(autostart.Path, "home", return_value=self.domicile), \
             mock.patch.object(autostart.sys, "platform", "darwin"):
            self.assertTrue(autostart.est_installe())


class TestsEstInstalleLinux(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_autostart_linux_")
        self.domicile = Path(self.temporaire.name)
        (self.domicile / ".config" / "systemd" / "user").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporaire.cleanup()

    def test_aucun_service_rend_faux(self):
        with mock.patch.object(autostart.Path, "home", return_value=self.domicile), \
             mock.patch.object(autostart.sys, "platform", "linux"):
            self.assertFalse(autostart.est_installe())

    def test_service_present_rend_vrai(self):
        cible = self.domicile / ".config" / "systemd" / "user" / f"{autostart.NOM}.service"
        cible.write_text("", encoding="utf-8")
        with mock.patch.object(autostart.Path, "home", return_value=self.domicile), \
             mock.patch.object(autostart.sys, "platform", "linux"):
            self.assertTrue(autostart.est_installe())


if __name__ == "__main__":
    unittest.main()
