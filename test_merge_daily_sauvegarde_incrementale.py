"""Fragilite trouvee en reel le 2026-08-20 (AUDIT-2026-08-13.md) : le
registre de normalisation n'etait sauvegarde qu'une fois par groupe
camera/jour complet, pas apres chaque clip. Une interruption (redemarrage,
plantage) en cours de journee perdait tout le progres deja acquis sur
cette meme journee, meme les clips deja correctement encodes juste avant -
constate sur une caméra reelle apres plusieurs redemarrages consecutifs :
les memes ~30 clips relancaient ffmpeg a chaque fois."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import merge_daily


class TestsSauvegardeIncrementale(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="blink_increment_test_")
        self.addCleanup(self.tmp.cleanup)
        self.racine = Path(self.tmp.name)
        for nom in ("clips", "normalized", "excluded", "daily", "weekly", "monthly"):
            (self.racine / nom).mkdir()
        self.registry_path = self.racine / "normalized" / merge_daily.NORMALIZED_STATE

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            input=self.racine / "clips", output=self.racine / "daily",
            weekly_output=self.racine / "weekly", monthly_output=self.racine / "monthly",
            normalized_output=self.racine / "normalized", excluded_output=self.racine / "excluded",
            no_weekly=True, no_monthly=True, no_timestamp=True,
            exclude=[], include=[], timezone="UTC", date=None, camera=None,
            force=False, font=None, preset="veryfast", crf=21,
        )

    def test_un_clip_reussi_reste_acquis_si_le_suivant_plante(self):
        source_a = self.racine / "clips" / "a.mp4"
        source_b = self.racine / "clips" / "b.mp4"
        source_a.write_bytes(b"    ftyp" + b"\x00" * 56)
        source_b.write_bytes(b"    ftyp" + b"\x00" * 56)
        created = dt.datetime(2026, 8, 13, 10, tzinfo=dt.timezone.utc)
        groupes = {("jardin", "2026-08-13"): [(created, source_a), (created, source_b)]}
        info = merge_daily.ClipInfo(created=created, source=source_a, duration=5.0,
                                    width=1920, height=1080, fps=30.0, has_audio=True)

        def normalize_simule(ffmpeg, timezone, registry, normalized_dir, identity, clip,
                             target, key, font_path, preset, crf, force, on_progress=None):
            if identity.endswith("a.mp4"):
                registry["clips"][identity] = {"key": key, "normalized_at": "now"}
                return True, "", True
            raise RuntimeError("plantage simulé après le premier clip")

        with mock.patch.object(merge_daily, "find_ffmpeg", return_value="ffmpeg"), \
             mock.patch.object(merge_daily, "load_groups", return_value=groupes), \
             mock.patch.object(merge_daily, "clip_info", return_value=info), \
             mock.patch.object(merge_daily, "camera_target", return_value=(1920, 1080, 30.0)), \
             mock.patch.object(merge_daily, "normalize_clip", side_effect=normalize_simule):
            with self.assertRaises(RuntimeError):
                merge_daily._executer(self._args())

        registre = json.loads(self.registry_path.read_text(encoding="utf-8"))
        identite_a = merge_daily.clip_identity(self.racine / "clips", source_a)
        self.assertIn(identite_a, registre.get("clips", {}))
        self.assertIn("key", registre["clips"][identite_a])


if __name__ == "__main__":
    unittest.main(verbosity=2)
