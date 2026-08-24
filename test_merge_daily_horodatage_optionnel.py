"""Non-régression de l'horodatage optionnel (AUDIT-2026-08-13.md, section
28.36) : le filtre drawtext, déjà omis pour les agrégats hebdo/mensuel via
font_path=None, devient aussi optionnel pour la normalisation elle-même,
réglable depuis la page web (--no-timestamp)."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8, édition Windows 7
    from backports.zoneinfo import ZoneInfo

import merge_daily as md


class TestsRenderKeyAvecEtSansHorodatage(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_render_key_")
        self.source = Path(self.temporaire.name) / "brut.mp4"
        self.source.write_bytes(b"\x00" * 16)

    def tearDown(self) -> None:
        self.temporaire.cleanup()

    def cle(self, font_path):
        clip = md.ClipInfo(
            created=dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc), source=self.source,
            duration=5.0, width=1920, height=1080, fps=30.0, has_audio=True,
        )
        return md.render_key(
            "jardin/clip.mp4", clip,
            (1920, 1080, 30.0), 1755500000, font_path, "veryfast", 21,
        )

    def test_ne_plante_pas_sans_police(self):
        # AttributeError avant le correctif : font_path.as_posix() sur None.
        self.cle(None)

    def test_bascule_horodatage_change_l_empreinte(self):
        avec = self.cle(Path("C:/Windows/Fonts/arialbd.ttf"))
        sans = self.cle(None)
        self.assertNotEqual(avec, sans,
                            "un clip deja normalise horodate doit se re-encoder "
                            "si l'horodatage est ensuite desactive, pas rester tel quel")

    def test_meme_police_meme_empreinte(self):
        police = Path("C:/Windows/Fonts/arialbd.ttf")
        self.assertEqual(self.cle(police), self.cle(police))


class TestsBuildBatchFilterSansHorodatage(unittest.TestCase):
    def clip(self):
        return md.ClipInfo(
            created=dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc),
            source=Path("brut.mp4"), duration=5.0, width=1920, height=1080,
            fps=30.0, has_audio=True,
        )

    def test_aucun_drawtext_sans_police(self):
        graphe = md.build_batch_filter(
            [self.clip()], 1920, 1080, 30.0, ZoneInfo("UTC"), None,
        )
        self.assertNotIn("drawtext", graphe)

    def test_drawtext_present_avec_police(self):
        graphe = md.build_batch_filter(
            [self.clip()], 1920, 1080, 30.0, ZoneInfo("UTC"),
            "'C\\:/Windows/Fonts/arialbd.ttf'",
        )
        self.assertIn("drawtext", graphe)


if __name__ == "__main__":
    unittest.main()
