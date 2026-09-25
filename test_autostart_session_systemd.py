"""Issue #23 : « systemctl --user » depuis un utilisateur dédié ouvert par su
ou sudo, sans XDG_RUNTIME_DIR ni DBUS_SESSION_BUS_ADDRESS. autostart les
déduit de /run/user/<uid> (ici un dossier temporaire), sinon il explique
comment démarrer le service sans connexion (loginctl enable-linger)."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-autostart-systemd-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import autostart  # noqa: E402 - environnement isolé avant import


class EnvSystemctl(unittest.TestCase):
    def setUp(self):
        dossier = tempfile.TemporaryDirectory(prefix="blink-run-user-")
        self.addCleanup(dossier.cleanup)
        self.run_user = Path(dossier.name)

    def session(self, uid=1001, bus=True) -> Path:
        dossier = self.run_user / str(uid)
        dossier.mkdir()
        if bus:
            (dossier / "bus").write_text("", encoding="utf-8")
        return dossier

    def test_session_deduite_de_run_user(self):
        dossier = self.session()
        env = autostart.env_systemctl({"PATH": "/usr/bin"}, uid=1001, racine=self.run_user)
        self.assertEqual(env["XDG_RUNTIME_DIR"], str(dossier))
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], f"unix:path={dossier / 'bus'}")
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_variables_deja_presentes_conservees(self):
        self.session()
        existant = {"XDG_RUNTIME_DIR": "/run/user/42",
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/42/bus"}
        env = autostart.env_systemctl(existant, uid=1001, racine=self.run_user)
        self.assertEqual(env, existant)

    def test_sans_bus_pas_d_adresse_inventee(self):
        self.session(bus=False)
        env = autostart.env_systemctl({}, uid=1001, racine=self.run_user)
        self.assertIn("XDG_RUNTIME_DIR", env)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)

    def test_sans_session_systemd_rien_d_invente(self):
        env = autostart.env_systemctl({}, uid=1001, racine=self.run_user)
        self.assertNotIn("XDG_RUNTIME_DIR", env)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)

    def installer(self, env: dict) -> tuple:
        with tempfile.TemporaryDirectory(prefix="blink-home-") as home, \
                mock.patch.object(autostart.Path, "home", return_value=Path(home)), \
                mock.patch.object(autostart, "env_systemctl", return_value=env), \
                mock.patch.object(autostart.runtime, "lancer") as lancer, \
                contextlib.redirect_stdout(io.StringIO()) as sortie:
            autostart._linux("on", False)
        return lancer, sortie.getvalue()

    def test_systemctl_recoit_l_environnement_de_session(self):
        env = {"XDG_RUNTIME_DIR": "/run/user/1001",
               "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1001/bus"}
        lancer, sortie = self.installer(env)
        self.assertEqual(lancer.call_count, 2)
        for appel in lancer.call_args_list:
            self.assertEqual(appel.args[0][:2], ["systemctl", "--user"])
            self.assertIs(appel.kwargs["env"], env)
        self.assertNotIn("loginctl", sortie)

    def test_sans_session_explique_enable_linger(self):
        _lancer, sortie = self.installer({})
        self.assertIn("loginctl enable-linger", sortie)


if __name__ == "__main__":
    unittest.main()
