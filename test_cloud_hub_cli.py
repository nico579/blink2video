"""Audit du 2026-10-02, B03 : `download --from cloud --hub X` ne téléchargeait
rien et répondait 0. La CLI passait `modules=[]` à toute source cloud, et le
filtre du moteur (réseaux autorisés par le hub) éliminait alors tous les clips.
Chaîne CLI complète (analyse des arguments, sélection des modules, moteur) ; seuls
le réseau Blink et le transfert sont simulés."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import blink_auth
import blink_cli
import blink_engine
import blink_models
import runtime
from test_blink_engine_notification_webhook import VIDEO


def clip_cloud(identifiant, camera, reseau):
    return blink_models.CloudClip({
        "id": identifiant, "device_name": camera, "network_id": reseau,
        "created_at": f"2026-09-07T10:0{identifiant % 10}:00+00:00",
        "media": "https://example.invalid/simule"})


class CloudHubCli(unittest.IsolatedAsyncioTestCase):
    async def lancer(self, clips, *options):
        self.transferts = 0
        sync = SimpleNamespace(network_id="7", sync_id="1")
        blink = SimpleNamespace(sync={})

        @contextlib.asynccontextmanager
        async def session():
            yield object()

        async def telecharger(_blink, destination):
            self.transferts += 1
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(VIDEO)
            return True

        with tempfile.TemporaryDirectory(prefix="blink-cloud-hub-") as dossier:
            racine = Path(dossier)
            for clip in clips:
                clip.download_to = telecharger
            with contextlib.ExitStack() as patches:
                patches.enter_context(mock.patch.object(runtime, "app_dir", return_value=racine))
                runtime.ecrire_suppression_auto(set())
                patches.enter_context(mock.patch.object(
                    blink_auth, "session_http_temporaire", session))
                patches.enter_context(mock.patch.object(
                    blink_auth, "connect", mock.AsyncMock(return_value=blink)))
                patches.enter_context(mock.patch.object(
                    blink_models, "select_sync_modules",
                    side_effect=lambda _b, nom=None: (
                        [("Maison", sync)] if nom in (None, "Maison")
                        else (_ for _ in ()).throw(ValueError(f"hub inconnu : {nom}")))))
                patches.enter_context(mock.patch.object(
                    blink_models, "read_cloud_manifest", mock.AsyncMock(return_value=clips)))
                patches.enter_context(mock.patch.object(
                    blink_engine.md, "valid_mp4_complet", side_effect=blink_engine.md.valid_mp4))
                for nom in ("bootstrap", "travail", "marquer", "toast", "notifier_nouveau_media"):
                    patches.enter_context(mock.patch.object(runtime, nom))
                patches.enter_context(mock.patch.object(runtime, "lire_langue", return_value="fr"))
                patches.enter_context(mock.patch.object(
                    runtime, "lire_reglages", return_value={"port": 8765}))
                sortie = io.StringIO()
                patches.enter_context(contextlib.redirect_stdout(sortie))
                argv = ["blink2video", "download", "--from", "cloud",
                        "--output", str(racine / "clips"), *options]
                with mock.patch.object(sys, "argv", argv):
                    args = blink_cli.parse_args()
                code = await blink_cli.main(args)
            return code, sortie.getvalue(), len(list((racine / "clips").rglob("*.mp4")))

    async def test_hub_choisi_telecharge_les_clips_de_son_reseau(self):
        code, _, mp4 = await self.lancer([clip_cloud(101, "Salon", 7)], "--hub", "Maison")
        self.assertEqual((code, self.transferts, mp4), (0, 1, 1))

    async def test_clip_d_un_autre_reseau_reste_exclu(self):
        clips = [clip_cloud(101, "Salon", 7), clip_cloud(102, "Garage", 99)]
        code, _, mp4 = await self.lancer(clips, "--hub", "Maison")
        self.assertEqual((code, self.transferts, mp4), (0, 1, 1))

    async def test_module_inconnu_est_signale_et_rien_n_est_telecharge(self):
        code, sortie, mp4 = await self.lancer([clip_cloud(101, "Salon", 7)], "--hub", "Nulle part")
        self.assertEqual(code, 2)
        self.assertEqual((self.transferts, mp4), (0, 0))
        self.assertIn("Nulle part", sortie)

    async def test_sans_hub_tous_les_clips_cloud_sont_pris(self):
        clips = [clip_cloud(101, "Salon", 7), clip_cloud(102, "Garage", 99)]
        code, _, mp4 = await self.lancer(clips)
        self.assertEqual((code, self.transferts, mp4), (0, 2, 2))


if __name__ == "__main__":
    unittest.main()
