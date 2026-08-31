"""Régressions de supervision et d'onboarding de ``blink_cli``.

Ces tests ne lancent aucun vrai processus et n'ouvrent aucun navigateur.
"""

from __future__ import annotations

import contextlib
import os
import unittest
from types import SimpleNamespace
from unittest import mock

import blink_cli
import tray


class _ProcessusTermine:
    def __init__(self, pid: int, code: int):
        self.pid = pid
        self.returncode = code

    def poll(self):
        return self.returncode


class TestsSupervision(unittest.TestCase):
    def test_le_code_du_dernier_processus_est_collecte(self):
        processus = [_ProcessusTermine(101, 3), _ProcessusTermine(102, 7)]
        with mock.patch.object(
            blink_cli.runtime, "verrou_controle",
            return_value=contextlib.nullcontext(),
        ), mock.patch.object(
            blink_cli.runtime, "inscrire_instance",
        ), mock.patch.object(
            blink_cli.runtime, "demarrer", side_effect=processus,
        ), mock.patch.object(
            blink_cli.runtime, "self_command", return_value=["faux"],
        ), mock.patch.object(
            blink_cli.runtime, "flags_enfant", return_value=0,
        ), mock.patch.object(
            blink_cli.runtime, "lire_reglages", return_value={"port": 8765},
        ), mock.patch.object(
            blink_cli.runtime, "demander_arret",
        ), mock.patch.object(
            blink_cli.runtime, "effacer_arret_demande",
        ), mock.patch.object(
            tray, "disponible", return_value=False,
        ), mock.patch.object(blink_cli.time, "sleep"):
            code = blink_cli.executer([
                ["serve"],
                ["download", "--loop", "1"],
            ])

        self.assertEqual(code, 7)

    def test_download_boucle_seul_est_inscrit_pour_stop(self):
        arguments = SimpleNamespace()
        with mock.patch.object(
            blink_cli.runtime, "verrou_controle",
            return_value=contextlib.nullcontext(),
        ), mock.patch.object(
            blink_cli.runtime, "inscrire_instance",
        ) as inscrire, mock.patch.object(
            blink_cli, "parse_args", return_value=arguments,
        ), mock.patch.object(
            blink_cli, "main", new=mock.AsyncMock(return_value=0),
        ):
            code = blink_cli.executer([["download", "--loop=1"]])

        self.assertEqual(code, 0)
        inscrire.assert_called_once_with([["download", "--loop=1"]])


class TestsOnboardingPort(unittest.TestCase):
    def test_port_forme_egale_est_sonde(self):
        processus = SimpleNamespace(pid=4242, poll=mock.Mock(return_value=None))
        with mock.patch.object(
            blink_cli.runtime, "demarrer", return_value=processus,
        ), mock.patch.object(
            blink_cli.runtime, "self_command",
            side_effect=lambda *arguments: list(arguments),
        ), mock.patch.object(
            blink_cli.runtime, "arreter_processus",
        ), mock.patch.object(
            blink_cli.runtime, "bootstrap",
        ), mock.patch.object(
            blink_cli, "_port_ouvert", return_value=True,
        ) as port_ouvert, mock.patch.dict(
            os.environ, {"BLINK_NO_BROWSER": "1"}, clear=False,
        ):
            code = blink_cli.accueillir(
                {"authenticated": True, "error": None},
                ["--port=55432"],
                delai=5,
            )

        self.assertEqual(code, 0)
        port_ouvert.assert_called_once_with(55432)


if __name__ == "__main__":
    unittest.main()
