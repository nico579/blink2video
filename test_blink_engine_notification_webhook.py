"""Webhook sortant (issue GitHub #11) : vérifie l'appel réel depuis
un_passage() pour les deux sources (USB et cloud), pas seulement par lecture
du code. Fixture calquée sur test_suppression_auto_pendant_lot.py, qui
exerce déjà ce même passage réussi pour les deux sources."""

import contextlib
import datetime as dt
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import blink_engine
import blink_models
import blink_registre
import runtime


def _boite(nom, contenu=b""):
    return (len(contenu) + 8).to_bytes(4, "big") + nom + contenu


VIDEO = (
    _boite(b"ftyp", b"isom\x00\x00\x02\x00isomiso2")
    + _boite(b"moov") + _boite(b"mdat", b"video-simulee")
)


class NotificationWebhookUnPassageTests(unittest.IsolatedAsyncioTestCase):
    async def _executer(self, source):
        with tempfile.TemporaryDirectory(prefix="blink-notif-webhook-") as dossier:
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

            clip = Clip(1, "Jardin")
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
                        blink_models, nom, new=mock.AsyncMock(return_value=[clip])))
                patches.enter_context(mock.patch.object(
                    blink_engine, "download_clip", side_effect=telecharger_usb))
                patches.enter_context(mock.patch.object(
                    blink_engine, "hub_lock", side_effect=lambda *_a: contextlib.nullcontext()))
                patches.enter_context(mock.patch.object(
                    blink_engine.md, "valid_mp4_complet",
                    side_effect=blink_engine.md.valid_mp4))
                patches.enter_context(mock.patch.object(runtime, "travail"))
                for nom in ("marquer", "toast"):
                    patches.enter_context(mock.patch.object(runtime, nom))
                patches.enter_context(mock.patch.object(
                    runtime, "lire_langue", return_value="fr"))
                patches.enter_context(mock.patch.object(
                    runtime, "lire_reglages", return_value={"port": 8765}))
                notifier = patches.enter_context(
                    mock.patch.object(runtime, "notifier_nouveau_media"))
                patches.enter_context(contextlib.redirect_stdout(io.StringIO()))
                code = await blink_engine.un_passage(
                    object(), arguments, [("Maison", sync)])
                self.assertEqual(code, 0)
                return notifier

    async def test_clip_usb_telecharge_declenche_la_notification(self):
        notifier = await self._executer("usb")
        notifier.assert_called_once()
        camera, chemin, type_media = notifier.call_args.args
        self.assertEqual(camera, "Jardin")
        self.assertEqual(type_media, "clip")
        self.assertTrue(str(chemin).endswith(".mp4"))

    async def test_clip_cloud_telecharge_declenche_la_notification(self):
        notifier = await self._executer("cloud")
        notifier.assert_called_once()
        camera, chemin, type_media = notifier.call_args.args
        self.assertEqual(camera, "Jardin")
        self.assertEqual(type_media, "clip")
        self.assertTrue(str(chemin).endswith(".mp4"))


if __name__ == "__main__":
    unittest.main()
