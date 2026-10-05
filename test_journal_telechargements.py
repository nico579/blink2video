"""Journal des téléchargements : chaque clip rapatrié laisse une ligne dans
telechargements.log, et la notification « N nouveaux clips » en laisse une aussi.
Né des « clips fantômes » : un toast annonçait deux clips que la page ne
montrait pas, et rien ne permettait de dire lesquels il annonçait.

Vrai passage un_passage() pour les deux sources, montage calqué sur
test_blink_engine_notification_webhook.py."""

import contextlib
import datetime as dt
import io
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import blink_engine
import blink_models
import runtime
from test_blink_engine_notification_webhook import VIDEO


class JournalTelechargementsUnPassage(unittest.IsolatedAsyncioTestCase):
    async def _passage(self, source, nombre=1):
        with tempfile.TemporaryDirectory(prefix="blink-journal-dl-") as dossier:
            racine = Path(dossier)
            sync = SimpleNamespace(sync_id=10, network_id=7)

            class Clip:
                def __init__(self, numero, camera):
                    self.id = str(numero)
                    self.name = camera
                    self.network_id = 7
                    self.device_id = "cam-" + camera
                    self.created_at = dt.datetime(
                        2026, 9, 7, 10, numero, tzinfo=dt.timezone.utc)
                    self.size = 1

                async def download_to(self, _blink, target):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(VIDEO)
                    return True

                async def delete_video(self, _blink):
                    return True

            clips = [Clip(n, "Jardin") for n in range(1, nombre + 1)]
            arguments = SimpleNamespace(
                since=None, camera=None, command="download",
                output=racine / "clips", hub=None, overwrite=False,
                source=source, loop=None,
            )

            async def telecharger_usb(blink, clip, target, _overwrite):
                await clip.download_to(blink, target)
                return "downloaded"

            with contextlib.ExitStack() as patches:
                patches.enter_context(mock.patch.object(
                    runtime, "app_dir", return_value=racine))
                runtime.ecrire_suppression_auto(set())
                for nom in ("read_local_manifest", "read_cloud_manifest"):
                    patches.enter_context(mock.patch.object(
                        blink_models, nom, new=mock.AsyncMock(return_value=clips)))
                patches.enter_context(mock.patch.object(
                    blink_engine, "download_clip", side_effect=telecharger_usb))
                patches.enter_context(mock.patch.object(
                    blink_engine, "hub_lock", side_effect=lambda *_a: contextlib.nullcontext()))
                patches.enter_context(mock.patch.object(
                    blink_engine.md, "valid_mp4_complet",
                    side_effect=blink_engine.md.valid_mp4))
                patches.enter_context(mock.patch.object(runtime, "travail"))
                for nom in ("marquer", "toast", "notifier_nouveau_media"):
                    patches.enter_context(mock.patch.object(runtime, nom))
                patches.enter_context(mock.patch.object(
                    runtime, "lire_langue", return_value="fr"))
                patches.enter_context(mock.patch.object(
                    runtime, "lire_reglages", return_value={"port": 8765}))
                patches.enter_context(contextlib.redirect_stdout(io.StringIO()))
                code = await blink_engine.un_passage(
                    object(), arguments, [("Maison", sync)])
            self.assertEqual(code, 0)
            journal = racine / blink_engine.JOURNAL_TELECHARGEMENTS
            lignes = journal.read_text(encoding="utf-8").splitlines() if journal.is_file() else []
            return lignes

    async def _verifier(self, source):
        lignes = await self._passage(source, nombre=2)
        telechargements = [l for l in lignes if f"  {source}  " in l]
        self.assertEqual(len(telechargements), 2, lignes)
        for ligne in telechargements:
            self.assertRegex(
                ligne,
                rf"^\d{{4}}-\d\d-\d\d \d\d:\d\d:\d\d  {source}  Jardin/.+\.mp4  {len(VIDEO)} o$")
        # La ligne de notification porte le même nombre que le toast.
        self.assertTrue(lignes[-1].endswith("  notification  2 clip(s) annoncé(s)"), lignes)
        # Aucun identifiant de compte ni URL dans le journal.
        self.assertFalse(any("http" in l or "network" in l for l in lignes))

    async def test_clips_usb_journalises(self):
        await self._verifier("usb")

    async def test_clips_cloud_journalises(self):
        await self._verifier("cloud")

    async def test_rien_a_telecharger_n_ecrit_rien(self):
        lignes = await self._passage("usb", nombre=0)
        self.assertEqual(lignes, [])


class AideJournal(unittest.TestCase):
    def test_chemin_hors_du_dossier_se_replie_sur_le_nom(self):
        with tempfile.TemporaryDirectory(prefix="blink-journal-dl-") as dossier:
            racine = Path(dossier)
            fichier = racine / "ailleurs" / "x.mp4"
            fichier.parent.mkdir()
            fichier.write_bytes(b"abc")
            with mock.patch.object(runtime, "app_dir", return_value=racine):
                blink_engine._journaliser_telechargement("usb", fichier, racine / "clips")
            ligne = (racine / blink_engine.JOURNAL_TELECHARGEMENTS).read_text(encoding="utf-8")
            self.assertTrue(re.search(r"  usb  x\.mp4  3 o\n$", ligne), ligne)

    def test_fichier_disparu_n_est_pas_fatal(self):
        with tempfile.TemporaryDirectory(prefix="blink-journal-dl-") as dossier:
            racine = Path(dossier)
            with mock.patch.object(runtime, "app_dir", return_value=racine):
                blink_engine._journaliser_telechargement(
                    "cloud", racine / "clips" / "a" / "b.mp4", racine / "clips")
            ligne = (racine / blink_engine.JOURNAL_TELECHARGEMENTS).read_text(encoding="utf-8")
            self.assertIn("  cloud  a/b.mp4  0 o", ligne)


if __name__ == "__main__":
    unittest.main()
