"""Une purge de Blink_Clips conserve l'historique et les notifications justes."""

import contextlib
import datetime as dt
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import blink_engine
import blink_models
import blink_registre
import runtime
from test_blink_engine_notification_webhook import VIDEO


class TestPurgeClips(unittest.IsolatedAsyncioTestCase):
    async def test_dossier_supprime_seul_le_vrai_nouveau_clip_est_annonce(self):
        with tempfile.TemporaryDirectory(prefix="blink-purge-") as temporaire:
            racine = Path(temporaire)
            sortie = racine / "Blink_Clips"
            sync = SimpleNamespace(sync_id=10, network_id=7)

            class Clip:
                def __init__(self, numero):
                    self.id = str(numero)
                    self.name = "Jardin"
                    self.network_id = 7
                    self.device_id = "camera-jardin"
                    self.created_at = dt.datetime(
                        2026, 10, 14, 12, numero, tzinfo=dt.timezone.utc,
                    )
                    self.size = len(VIDEO)

            ancien, nouveau = Clip(1), Clip(2)
            manifeste = [ancien]
            arguments = SimpleNamespace(
                since=None, camera=None, command="download", output=sortie,
                hub=None, overwrite=False, source="usb", loop=None,
            )
            appels = []

            async def telecharger(_blink, clip, cible, _overwrite):
                appels.append(clip.id)
                cible.parent.mkdir(parents=True, exist_ok=True)
                cible.write_bytes(VIDEO)
                return "downloaded"

            with contextlib.ExitStack() as patches:
                patches.enter_context(mock.patch.object(runtime, "app_dir", return_value=racine))
                patches.enter_context(mock.patch.object(
                    blink_models, "read_local_manifest",
                    new=mock.AsyncMock(side_effect=lambda _sync: list(manifeste)),
                ))
                patches.enter_context(mock.patch.object(
                    blink_models, "read_cloud_manifest",
                    new=mock.AsyncMock(return_value=[]),
                ))
                patches.enter_context(mock.patch.object(
                    blink_engine, "download_clip", side_effect=telecharger,
                ))
                patches.enter_context(mock.patch.object(
                    blink_engine, "hub_lock", side_effect=lambda *_a: contextlib.nullcontext(),
                ))
                patches.enter_context(mock.patch.object(runtime, "travail"))
                patches.enter_context(mock.patch.object(runtime, "marquer"))
                toast = patches.enter_context(mock.patch.object(runtime, "toast"))
                patches.enter_context(mock.patch.object(runtime, "notifier_nouveau_media"))
                patches.enter_context(mock.patch.object(runtime, "lire_langue", return_value="fr"))
                patches.enter_context(mock.patch.object(runtime, "lire_reglages", return_value={"port": 8765}))
                patches.enter_context(contextlib.redirect_stdout(io.StringIO()))

                self.assertEqual(await blink_engine.un_passage(
                    object(), arguments, [("Maison", sync)]), 0)
                self.assertEqual(appels, ["1"])
                toast.reset_mock()

                shutil.rmtree(sortie)
                self.assertTrue(blink_registre._copie_registre(sortie).is_file())
                manifeste.append(nouveau)
                self.assertEqual(await blink_engine.un_passage(
                    object(), arguments, [("Maison", sync)]), 0)

            self.assertEqual(appels, ["1", "2"])
            toast.assert_called_once()
            self.assertIn("1 nouveau clip", toast.call_args.args[1])
