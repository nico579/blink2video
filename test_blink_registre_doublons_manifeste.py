"""Non-régression de l'issue #151 : copies multiples d'un clip dans un manifeste USB.

Après une réindexation, le Sync Module 2 peut annoncer le même enregistrement
sous plusieurs IDs de manifeste. L'identité USB (``state_key``) ignore cet ID :
sans repli, les copies non appariées étaient téléchargées comme de nouveaux
clips, produisant des fichiers byte-identiques.

Exécution :

    python -B -m unittest -v test_blink_registre_doublons_manifeste.py
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


os.environ["BLINK_BOOTSTRAP"] = "none"

import blink_engine  # noqa: E402 - bootstrap neutralisé avant import
import blink_models  # noqa: E402
import blink_registre  # noqa: E402
import runtime  # noqa: E402


INSTANT = dt.datetime(2026, 11, 23, 12, 3, 6, tzinfo=dt.timezone.utc)


def boite_mp4(nom: bytes, contenu: bytes = b"") -> bytes:
    return (len(contenu) + 8).to_bytes(4, "big") + nom + contenu


VIDEO_VALIDE = (
    boite_mp4(b"ftyp", b"isom\x00\x00\x02\x00isomiso2")
    + boite_mp4(b"moov")
    + boite_mp4(b"mdat", b"paquet-video")
)


class FauxSync:
    def __init__(self, identifiant="hub-1", reseau="reseau-1"):
        self.sync_id = identifiant
        self.network_id = reseau


class FauxClip:
    def __init__(self, identifiant, camera="Mini_Cam", instant=INSTANT,
                 appareil="camera-7"):
        self.id = identifiant
        self.name = camera
        self.created_at = instant
        self.network_id = "reseau-1"
        self.device_id = appareil
        self.size = 1
        self.download_issue = None


class DeduplicationManifesteTests(unittest.TestCase):
    def test_copies_du_meme_clip_sous_des_ids_differents_sont_ecartees(self):
        sync = FauxSync()
        copies = [FauxClip(1605881221), FauxClip(4192489081), FauxClip(3634105320)]

        uniques, doublons = blink_registre.dedupliquer_manifeste_usb(sync, copies)

        self.assertEqual(doublons, 2)
        self.assertEqual([clip.id for clip in uniques], [1605881221])
        self.assertEqual(
            len({blink_registre.state_key(sync, clip) for clip in copies}), 1,
        )

    def test_clips_reellement_distincts_sont_conserves(self):
        sync = FauxSync()
        base = FauxClip(1)
        clips = [
            base,
            # Une seconde plus tard : le cas légitime que l'appariement
            # tolérant doit continuer à distinguer.
            FauxClip(2, instant=INSTANT + dt.timedelta(seconds=1)),
            # Même instant, autre appareil physique (donc autre caméra).
            FauxClip(3, camera="Jardin", appareil="camera-8"),
        ]

        uniques, doublons = blink_registre.dedupliquer_manifeste_usb(sync, clips)

        self.assertEqual(doublons, 0)
        self.assertEqual(uniques, clips)


class TelechargementManifesteDupliqueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_issue151_")
        self.home = Path(self.temporaire.name)
        self.ancien_home = os.environ.get("BLINK_HOME")
        os.environ["BLINK_HOME"] = str(self.home)
        self.sonde_media = mock.patch.object(
            blink_engine.md, "valid_mp4_complet",
            side_effect=blink_engine.md.valid_mp4,
        )
        self.sonde_media.start()
        self.addCleanup(self.sonde_media.stop)

    async def asyncTearDown(self):
        if self.ancien_home is None:
            os.environ.pop("BLINK_HOME", None)
        else:
            os.environ["BLINK_HOME"] = self.ancien_home
        self.temporaire.cleanup()

    def _arguments(self, sortie: Path) -> SimpleNamespace:
        return SimpleNamespace(
            since=None,
            camera=None,
            command="download",
            output=sortie,
            hub=None,
            overwrite=False,
            source="usb",
            loop=None,
        )

    async def _passage(self, sync, copies, sortie):
        telecharges = []

        async def telecharger(_blink, clip, cible, _overwrite):
            telecharges.append(clip.id)
            cible.parent.mkdir(parents=True, exist_ok=True)
            cible.write_bytes(VIDEO_VALIDE)
            return "downloaded"

        with mock.patch.object(
            blink_models, "read_local_manifest",
            new=mock.AsyncMock(return_value=copies),
        ), mock.patch.object(
            blink_engine, "download_clip", side_effect=telecharger,
        ), mock.patch.object(
            blink_engine, "_preparer_vignettes",
        ), mock.patch.object(
            runtime, "travail",
        ), mock.patch.object(
            runtime, "lire_suppression_auto", return_value=set(),
        ), mock.patch.object(runtime, "marquer"), \
             mock.patch.object(runtime, "toast"), \
             mock.patch.object(runtime, "lire_langue", return_value="fr"), \
             mock.patch.object(runtime, "lire_reglages", return_value={"port": 8765}), \
             contextlib.redirect_stdout(io.StringIO()) as sortie_console:
            code = await blink_engine.un_passage(
                object(), self._arguments(sortie), [("Maison", sync)],
            )
        return code, telecharges, sortie_console.getvalue()

    async def test_un_manifeste_duplique_ne_telecharge_qu_une_copie(self):
        sync = FauxSync()
        sortie = self.home / "clips"
        copies = [FauxClip(1605881221), FauxClip(4192489081), FauxClip(3634105320)]

        code, telecharges, journal = await self._passage(sync, copies, sortie)

        self.assertEqual(code, 0)
        self.assertEqual(telecharges, [1605881221])
        fichiers = list(sortie.rglob("*.mp4"))
        self.assertEqual(len(fichiers), 1)
        # Le glitch du module doit rester visible dans la sortie.
        self.assertIn("2 copie(s)", journal)
        registre = blink_registre.load_download_state(sortie)
        self.assertEqual(len(registre["clips"]), 1)

    async def test_un_clip_deja_au_registre_ne_retente_pas_ses_copies(self):
        sync = FauxSync()
        sortie = self.home / "clips"
        premiere = FauxClip(1605881221)
        cible = blink_models.target_path(sortie, premiere, sync=sync, source="usb")
        cible.parent.mkdir(parents=True)
        cible.write_bytes(VIDEO_VALIDE)
        etat = blink_registre.load_download_state(sortie)
        blink_registre.remember_download(
            etat, sync, "Maison", premiere, sortie, cible, source="usb",
        )
        blink_registre.save_download_state(sortie, etat)
        copies = [premiere, FauxClip(4192489081), FauxClip(3634105320)]

        code, telecharges, _journal = await self._passage(sync, copies, sortie)

        self.assertEqual(code, 0)
        self.assertEqual(telecharges, [])
        self.assertEqual(len(list(sortie.rglob("*.mp4"))), 1)


if __name__ == "__main__":
    unittest.main()
