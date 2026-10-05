"""Backlog « vignettes : les fabriquer au téléchargement du clip » (test à froid de
Joël, PR #59 : 2 672 clips, cache vide, 2 673 lancements de ffmpeg à l'ouverture).
Le téléchargeur fabrique maintenant la vignette de chaque clip qui arrive, au
même endroit et sous le même nom que serve.py : la page la trouve prête.

Vrai passage un_passage() pour les deux sources, ffmpeg simulé."""

import contextlib
import datetime as dt
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("BLINK_BOOTSTRAP", "none")
_HOME = tempfile.TemporaryDirectory(prefix="blink-vignettes-dl-")
os.environ.setdefault("BLINK_HOME", _HOME.name)

import blink_engine  # noqa: E402
import blink_models  # noqa: E402
import merge_daily as md  # noqa: E402
import runtime  # noqa: E402
import serve  # noqa: E402
from test_blink_engine_notification_webhook import VIDEO  # noqa: E402


class ExtraireVignette(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="blink-extraire-")
        self.addCleanup(self.tmp.cleanup)
        self.pending = Path(self.tmp.name) / "v.tmp.jpg"
        self.source = Path(self.tmp.name) / "clip.mp4"
        self.source.write_bytes(b"x")
        self.commandes = []

    def lancer(self, reussit_au_rang):
        def faux(commande, **_options):
            self.commandes.append(commande)
            if len(self.commandes) >= reussit_au_rang:
                Path(commande[-1]).write_bytes(b"jpeg")
            return mock.Mock(returncode=0)
        return mock.patch.object(runtime, "lancer", side_effect=faux)

    def test_premiere_tentative_apres_le_debut(self):
        with self.lancer(1):
            self.assertTrue(md.extraire_vignette("ffmpeg", self.source, self.pending))
        self.assertEqual(len(self.commandes), 1)
        self.assertIn("1.5", self.commandes[0])

    def test_clip_court_se_rabat_sur_la_premiere_image(self):
        with self.lancer(2):
            self.assertTrue(md.extraire_vignette("ffmpeg", self.source, self.pending))
        self.assertEqual(len(self.commandes), 2)
        self.assertNotIn("-ss", self.commandes[1])

    def test_echec_total_rend_faux(self):
        with self.lancer(99):
            self.assertFalse(md.extraire_vignette("ffmpeg", self.source, self.pending))
        self.assertFalse(self.pending.exists())

    def test_le_delai_est_transmis(self):
        with self.lancer(1) as lancer:
            md.extraire_vignette("ffmpeg", self.source, self.pending, timeout=60)
        self.assertEqual(lancer.call_args.kwargs["timeout"], 60)


class VignettesAuTelechargement(unittest.IsolatedAsyncioTestCase):
    async def _passage(self, source, nombre=2, ffmpeg="ffmpeg-simule", echec_ffmpeg=False):
        self.tmp = tempfile.TemporaryDirectory(prefix="blink-vignettes-passage-")
        self.addCleanup(self.tmp.cleanup)
        racine = Path(self.tmp.name)
        sync = SimpleNamespace(sync_id=10, network_id=7)

        class Clip:
            def __init__(self, numero):
                self.id = str(numero)
                self.name = "Jardin"
                self.network_id = 7
                self.device_id = "cam-Jardin"
                self.created_at = dt.datetime(
                    2026, 9, 7, 10, numero % 60, numero // 60, tzinfo=dt.timezone.utc)
                self.size = 1

            async def download_to(self, _blink, target):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(VIDEO)
                return True

            async def delete_video(self, _blink):
                return True

        clips = [Clip(n) for n in range(1, nombre + 1)]
        arguments = SimpleNamespace(
            since=None, camera=None, command="download", output=racine / "clips",
            hub=None, overwrite=False, source=source, loop=None)
        self.lancements = []
        self.vignettes_au_toast = None
        self.racine = racine

        def faux_ffmpeg(commande, **_options):
            self.lancements.append(commande)
            if not echec_ffmpeg:
                Path(commande[-1]).write_bytes(b"jpeg")
            return mock.Mock(returncode=0)

        def toast(*_a, **_k):
            self.vignettes_au_toast = sorted(
                (racine / ".blink_thumbs").rglob("*.jpg")) if (racine / ".blink_thumbs").exists() else []

        async def telecharger_usb(blink, clip, target, _overwrite):
            await clip.download_to(blink, target)
            return "downloaded"

        blink_engine._VIGNETTES_EN_ATTENTE.clear()
        with contextlib.ExitStack() as patches:
            patches.enter_context(mock.patch.object(runtime, "app_dir", return_value=racine))
            runtime.ecrire_suppression_auto(set())
            for nom in ("read_local_manifest", "read_cloud_manifest"):
                patches.enter_context(mock.patch.object(
                    blink_models, nom, new=mock.AsyncMock(return_value=clips)))
            patches.enter_context(mock.patch.object(
                blink_engine, "download_clip", side_effect=telecharger_usb))
            patches.enter_context(mock.patch.object(
                blink_engine, "hub_lock", side_effect=lambda *_a: contextlib.nullcontext()))
            patches.enter_context(mock.patch.object(
                blink_engine.md, "valid_mp4_complet", side_effect=blink_engine.md.valid_mp4))
            patches.enter_context(mock.patch.object(
                blink_engine, "_ffmpeg_pour_vignettes", return_value=ffmpeg))
            patches.enter_context(mock.patch.object(runtime, "lancer", side_effect=faux_ffmpeg))
            patches.enter_context(mock.patch.object(runtime, "travail"))
            patches.enter_context(mock.patch.object(runtime, "marquer"))
            patches.enter_context(mock.patch.object(runtime, "notifier_nouveau_media"))
            patches.enter_context(mock.patch.object(runtime, "toast", side_effect=toast))
            patches.enter_context(mock.patch.object(runtime, "lire_langue", return_value="fr"))
            patches.enter_context(mock.patch.object(
                runtime, "lire_reglages", return_value={"port": 8765}))
            patches.enter_context(contextlib.redirect_stdout(io.StringIO()))
            code = await blink_engine.un_passage(object(), arguments, [("Maison", sync)])
        self.assertEqual(code, 0)
        return sorted((racine / ".blink_thumbs").rglob("*.jpg")) \
            if (racine / ".blink_thumbs").exists() else []

    async def test_une_vignette_par_clip_usb(self):
        vignettes = await self._passage("usb", nombre=2)
        self.assertEqual(len(vignettes), 2)
        for v in vignettes:
            self.assertEqual(v.relative_to(self.racine / ".blink_thumbs").parts[0], "clip", v)
            self.assertEqual(v.read_bytes(), b"jpeg")

    async def test_une_vignette_par_clip_cloud(self):
        self.assertEqual(len(await self._passage("cloud", nombre=2)), 2)

    async def test_les_vignettes_existent_avant_la_notification(self):
        await self._passage("usb", nombre=2)
        self.assertEqual(len(self.vignettes_au_toast), 2)

    async def test_serve_les_trouve_fraiches_et_ne_lance_pas_ffmpeg(self):
        vignettes = await self._passage("usb", nombre=1)
        racine = self.racine
        source = next((racine / "clips").rglob("*.mp4"))
        identite = md.clip_identity(racine / "clips", source)
        self.assertEqual(
            vignettes, [(racine / ".blink_thumbs" / "clip" / identite).with_suffix(".jpg")])
        h = serve.Handler.__new__(serve.Handler)
        h.paths = {"thumbs": racine / ".blink_thumbs"}
        h.ffmpeg = "ffmpeg-simule"
        for nom in ("send_error", "send_response", "send_header", "end_headers"):
            setattr(h, nom, mock.Mock())
        h.wfile = mock.Mock()
        h.headers = {}
        h.close_connection = False
        h._client_parti = mock.Mock(return_value=False)
        with mock.patch.object(runtime, "lancer") as lancer:
            h.send_thumb(f"clip/{identite}", source)
        lancer.assert_not_called()
        h.send_response.assert_called_with(200)

    async def test_ffmpeg_en_echec_ne_casse_pas_le_passage(self):
        self.assertEqual(await self._passage("usb", nombre=2, echec_ffmpeg=True), [])

    async def test_sans_ffmpeg_aucune_vignette_et_aucun_lancement(self):
        vignettes = await self._passage("usb", nombre=2, ffmpeg=None)
        self.assertEqual(vignettes, [])
        self.assertEqual(self.lancements, [])

    async def test_au_plus_quarante_vignettes_par_passage(self):
        vignettes = await self._passage("usb", nombre=45)
        self.assertEqual(len(vignettes), blink_engine.MAX_VIGNETTES_PAR_PASSAGE)
        # Le reste n'attend pas : la file est vidée, pas reportée sans fin.
        self.assertEqual(blink_engine._VIGNETTES_EN_ATTENTE, [])

    async def test_rien_a_telecharger_ne_lance_rien(self):
        await self._passage("usb", nombre=0)
        self.assertEqual(self.lancements, [])


if __name__ == "__main__":
    unittest.main()
