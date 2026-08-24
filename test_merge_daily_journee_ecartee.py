"""Bug #2, revue de code du 0eab463 (AUDIT-2026-08-13.md, 28.61) :
load_groups() faisait disparaître une journée entièrement écartée au lieu
de la lister avec une liste vide, laissant sa vidéo déjà assemblée sur
disque indéfiniment, jamais revue ni supprimée."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8, édition Windows 7
    from backports.zoneinfo import ZoneInfo

import merge_daily


class TestsJourneeEcartee(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.input_dir = Path(self.tmp.name)
        self.tz = ZoneInfo("Europe/Paris")

    def _ecrire_registre(self, clips: dict) -> None:
        chemin = self.input_dir / merge_daily.DOWNLOAD_STATE
        chemin.write_text(json.dumps({"clips": clips}), encoding="utf-8")

    def test_journee_entierement_ecartee_reste_listee_vide(self):
        self._ecrire_registre({
            "a": {"path": "jardin/a.mp4", "camera": "jardin", "excluded": True,
                  "created_at": "2026-08-10T10:00:00+00:00"},
        })
        groupes = merge_daily.load_groups(self.input_dir, self.tz)
        self.assertIn(("jardin", "2026-08-10"), groupes)
        self.assertEqual(groupes[("jardin", "2026-08-10")], [])

    def test_journee_partiellement_ecartee_ignore_pas_le_reste(self):
        clip_valide = self.input_dir / "jardin" / "b.mp4"
        clip_valide.parent.mkdir(parents=True)
        clip_valide.write_bytes(b"    ftyp" + b"\x00" * 56)
        self._ecrire_registre({
            "a": {"path": "jardin/a.mp4", "camera": "jardin", "excluded": True,
                  "created_at": "2026-08-10T09:00:00+00:00"},
            "b": {"path": "jardin/b.mp4", "camera": "jardin", "excluded": False,
                  "created_at": "2026-08-10T10:00:00+00:00"},
        })
        groupes = merge_daily.load_groups(self.input_dir, self.tz)
        self.assertEqual(len(groupes[("jardin", "2026-08-10")]), 1)
        self.assertEqual(groupes[("jardin", "2026-08-10")][0][1], clip_valide.resolve())

    def test_journee_avec_clip_invalide_reste_listee_vide(self):
        # Chemin absent du disque : simule un clip disparu ou jamais
        # tombé, sans passer par « excluded » - même conséquence attendue.
        self._ecrire_registre({
            "a": {"path": "jardin/absent.mp4", "camera": "jardin", "excluded": False,
                  "created_at": "2026-08-10T10:00:00+00:00"},
        })
        groupes = merge_daily.load_groups(self.input_dir, self.tz)
        self.assertIn(("jardin", "2026-08-10"), groupes)
        self.assertEqual(groupes[("jardin", "2026-08-10")], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
