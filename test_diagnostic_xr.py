"""Rapport XR transmissible : lecture seule, résultat utile et données privées absentes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import diagnostic_xr


class FauxSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class DiagnosticXRTests(unittest.IsolatedAsyncioTestCase):
    async def test_manifest_accessible_produit_un_rapport_anonyme(self):
        storage = {"compatible": True, "enabled": True, "status": True}
        sync = SimpleNamespace(
            network_id=7,
            sync_id=900,
            serial="SECRET-SERIAL",
            name="Jardin privé",
            _local_storage=storage,
            local_storage=True,
        )
        blink = SimpleNamespace(
            homescreen={
                "networks": [{"id": 7, "name": "Jardin privé"}],
                "sync_modules": [{"id": 900, "serial": "SECRET-SERIAL"}],
                "cameras": [],
                "owls": [],
                "doorbells": [{"name": "Porte secrète"}],
            },
            sync={"Porte secrète": SimpleNamespace()},
        )
        clips = [SimpleNamespace(name="Porte secrète"), SimpleNamespace(name="Porte secrète")]
        read_manifest = mock.AsyncMock(return_value=clips)

        code, lines = await diagnostic_xr.inspect_blink(
            blink,
            select_modules=lambda _blink, _name: [("Jardin privé", sync)],
            read_manifest=read_manifest,
        )
        report = "\n".join(lines)

        self.assertEqual(code, 0)
        self.assertIn("manifest: OK", report)
        self.assertIn("clips reported: 2", report)
        self.assertIn("Overall result: PASS_WITH_CLIPS", report)
        for private in ("Jardin privé", "Porte secrète", "SECRET-SERIAL"):
            self.assertNotIn(private, report)
        self.assertNotIn("900", report)
        self.assertNotIn("network identifier: 7", report)
        read_manifest.assert_awaited_once_with(sync)

    async def test_manifeste_vide_valide_la_route_sans_inventer_de_clips(self):
        sync = SimpleNamespace(
            network_id=7,
            sync_id=900,
            _local_storage={"compatible": True, "enabled": True},
            local_storage=True,
        )

        code, lines = await diagnostic_xr.inspect_blink(
            SimpleNamespace(homescreen={}, sync={}),
            select_modules=lambda _blink, _name: [("secret", sync)],
            read_manifest=mock.AsyncMock(return_value=[]),
        )

        self.assertEqual(code, 0)
        self.assertIn("PASS_EMPTY", "\n".join(lines))

    async def test_reporteur_recoit_le_vrai_module_homescreen_pas_la_doorbell(self):
        from test_xr_local_storage import compte_avec_stockage_local

        blink, doorbell = compte_avec_stockage_local()
        read_manifest = mock.AsyncMock(return_value=[])
        with mock.patch("blink_models.read_local_manifest", new=read_manifest):
            code, lines = await diagnostic_xr.inspect_blink(blink)

        selected = read_manifest.await_args.args[0]
        self.assertEqual(code, 0)
        self.assertIsNot(selected, doorbell)
        self.assertEqual(str(selected.sync_id), "900")
        report = "\n".join(lines)
        self.assertNotIn("Jardin", report)
        self.assertNotIn("Porte d'entrée", report)
        self.assertNotIn("900", report)

    async def test_aucun_module_est_un_echec_lisible(self):
        blink = SimpleNamespace(homescreen={}, sync={})

        code, lines = await diagnostic_xr.inspect_blink(
            blink,
            select_modules=lambda _blink, _name: [],
            read_manifest=mock.AsyncMock(),
        )

        self.assertEqual(code, 1)
        self.assertIn("no Sync Module", "\n".join(lines))

    async def test_session_absente_ne_demande_jamais_des_identifiants(self):
        async def connector_with_noisy_output(_session):
            print("private@example.test SECRET_AUTH_TOKEN_123456789")
            return None

        connector = mock.AsyncMock(side_effect=connector_with_noisy_output)

        code, report = await diagnostic_xr.collect_report(
            session_factory=FauxSession,
            connector=connector,
        )

        self.assertEqual(code, 2)
        self.assertIn("LOGIN REQUIRED", report)
        self.assertNotIn("private@example.test", report)
        self.assertNotIn("SECRET_AUTH_TOKEN", report)
        connector.assert_awaited_once()

    async def test_erreur_est_redigee_avant_le_rapport(self):
        message = (
            "failure for jane@example.com at C:\\Users\\Jane\\blink_auth.json "
            "https://example.test/path?token=secret "
            "abcdefghijklmnopqrstuvwxyz0123456789"
        )
        blink = SimpleNamespace(homescreen={}, sync={})

        code, lines = await diagnostic_xr.inspect_blink(
            blink,
            select_modules=mock.Mock(side_effect=RuntimeError(message)),
            read_manifest=mock.AsyncMock(),
        )
        report = "\n".join(lines)

        self.assertEqual(code, 1)
        self.assertIn("reference", report)
        self.assertNotIn("jane@example.com", report)
        self.assertNotIn("blink_auth.json", report)
        self.assertNotIn("example.test", report)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", report)

    async def test_ecriture_atomique_remplace_le_rapport(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / diagnostic_xr.REPORT_NAME
            path.write_text("old", encoding="utf-8")

            diagnostic_xr.write_report(path, "new report\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "new report\n")
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    async def test_dossier_installation_non_inscriptible_se_replie_dans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            fallback = Path(directory) / diagnostic_xr.REPORT_NAME
            with mock.patch.object(
                diagnostic_xr, "write_report", side_effect=[PermissionError(), None],
            ) as write, mock.patch.object(
                diagnostic_xr.tempfile, "gettempdir", return_value=directory,
            ):
                selected = diagnostic_xr.write_report_with_fallback(
                    Path("C:/Program Files/blink") / diagnostic_xr.REPORT_NAME,
                    "report",
                )

        self.assertEqual(selected, fallback)
        self.assertEqual(write.call_count, 2)

    def test_main_fonctionne_sans_stdout_dans_le_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / diagnostic_xr.REPORT_NAME
            with mock.patch.object(diagnostic_xr.runtime, "frozen", return_value=True), \
                    mock.patch.object(
                        diagnostic_xr, "collect_report",
                        new=mock.AsyncMock(return_value=(0, "safe report\n")),
                    ), mock.patch.object(
                        diagnostic_xr, "report_path", return_value=path,
                    ), mock.patch.object(
                        diagnostic_xr, "_show_message",
                    ), mock.patch.object(
                        diagnostic_xr, "_open_report",
                    ) as open_report, mock.patch.object(
                        diagnostic_xr.os, "environ", {},
                    ), mock.patch.object(
                        diagnostic_xr.sys, "stdout", None,
                    ), mock.patch.object(diagnostic_xr.sys, "stderr", None):
                code = diagnostic_xr.main()

            self.assertEqual(code, 0)
            self.assertEqual(path.read_text(encoding="utf-8"), "safe report\n")
            open_report.assert_called_once_with(path)


if __name__ == "__main__":
    unittest.main()
