"""Bug #11, revue de code du 0eab463 (AUDIT-2026-08-13.md, 28.68) :
last_clip_per_camera() ignorait les clips ecartes pour la derniere
activite (fausse alerte de silence possible), et compare() ne
declenchait aucune alerte batterie quand la premiere observation d'une
camera la montrait deja faible. Meme defaut retrouve et corrige ensuite
pour l'armement et le silence prolonge (revue de l'audit non date, section
« Autres bugs confirmes »)."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8, édition Windows 7
    from backports.zoneinfo import ZoneInfo

import merge_daily as md
import watch


class TestsDerniereActivite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.patch = mock.patch.object(watch, "BASE_DIR", self.base)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def _ecrire_registre(self, clips: dict) -> None:
        chemin = self.base / "Blink_Clips" / md.DOWNLOAD_STATE
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_text(json.dumps({"clips": clips}), encoding="utf-8")

    def test_clip_ecarte_compte_quand_meme_pour_la_derniere_activite(self):
        self._ecrire_registre({
            "a": {"path": "jardin/a.mp4", "camera": "jardin", "excluded": True,
                  "created_at": "2026-08-19T10:00:00+00:00"},
        })
        derniere = watch.last_clip_per_camera(ZoneInfo("UTC"))
        self.assertIn("jardin", derniere)
        self.assertTrue(derniere["jardin"].startswith("2026-08-19"))

    def test_clip_ecarte_plus_recent_l_emporte_sur_un_clip_garde_plus_ancien(self):
        self._ecrire_registre({
            "ancien": {"path": "jardin/a.mp4", "camera": "jardin", "excluded": False,
                       "created_at": "2026-08-10T10:00:00+00:00"},
            "recent_ecarte": {"path": "jardin/b.mp4", "camera": "jardin",
                              "excluded": True,
                              "created_at": "2026-08-19T10:00:00+00:00"},
        })
        derniere = watch.last_clip_per_camera(ZoneInfo("UTC"))
        self.assertTrue(derniere["jardin"].startswith("2026-08-19"))


class TestsAlerteBatterie(unittest.TestCase):
    def _etat(self, battery: str) -> dict:
        return {"cameras": {"jardin": {"online": True, "armed": True,
                                       "battery": battery, "system_armed": True}},
                "modules": [], "last_clip": {}}

    def test_batterie_deja_faible_au_premier_passage_alerte(self):
        """Aucun etat precedent (premiere observation) : la batterie
        montree faible dès le depart doit alerter, pas seulement une
        transition depuis "ok"."""
        alerts, _ = watch.compare({}, self._etat("low"), ZoneInfo("UTC"), set())
        self.assertTrue(any("batterie" in a for a in alerts))

    def test_batterie_ok_puis_faible_alerte_toujours(self):
        alerts, _ = watch.compare(self._etat("ok"), self._etat("low"),
                                  ZoneInfo("UTC"), set())
        self.assertTrue(any("batterie" in a for a in alerts))

    def test_batterie_faible_puis_toujours_faible_n_alerte_pas_deux_fois(self):
        alerts, _ = watch.compare(self._etat("low"), self._etat("low"),
                                  ZoneInfo("UTC"), set())
        self.assertFalse(any("batterie" in a for a in alerts))

    def test_batterie_ok_au_premier_passage_n_alerte_pas(self):
        alerts, _ = watch.compare({}, self._etat("ok"), ZoneInfo("UTC"), set())
        self.assertFalse(any("batterie" in a for a in alerts))


class TestsAlerteDetectionCoupee(unittest.TestCase):
    """Même défaut que la batterie (bug #11), pour l'armement cette fois."""

    def _etat(self, armed: bool) -> dict:
        return {"cameras": {"jardin": {"online": True, "armed": armed,
                                       "battery": "ok", "system_armed": armed}},
                "modules": [], "last_clip": {}}

    def test_deja_desarmee_au_premier_passage_alerte(self):
        alerts, _ = watch.compare({}, self._etat(False), ZoneInfo("UTC"), set())
        self.assertTrue(any("détection" in a for a in alerts))

    def test_armee_puis_desarmee_alerte(self):
        alerts, _ = watch.compare(self._etat(True), self._etat(False),
                                  ZoneInfo("UTC"), set())
        self.assertTrue(any("détection" in a for a in alerts))

    def test_desarmee_puis_toujours_desarmee_n_alerte_pas_deux_fois(self):
        alerts, _ = watch.compare(self._etat(False), self._etat(False),
                                  ZoneInfo("UTC"), set())
        self.assertFalse(any("détection" in a for a in alerts))

    def test_armee_au_premier_passage_n_alerte_pas(self):
        alerts, _ = watch.compare({}, self._etat(True), ZoneInfo("UTC"), set())
        self.assertFalse(any("détection" in a for a in alerts))


class TestsAlerteSilence(unittest.TestCase):
    """Même défaut que la batterie (bug #11), pour le silence prolongé :
    un « at » précédent absent retombait sur now(), masquant un premier
    passage déjà silencieux depuis longtemps."""

    @staticmethod
    def _cameras() -> dict:
        return {"jardin": {"online": True, "armed": True, "battery": "ok",
                           "system_armed": True}}

    def test_deja_silencieuse_au_premier_passage_alerte(self):
        now = dt.datetime.now(ZoneInfo("UTC"))
        iso = (now - dt.timedelta(days=5)).isoformat()
        current = {"cameras": self._cameras(), "modules": [],
                  "last_clip": {"jardin": iso}}
        alerts, _ = watch.compare({}, current, ZoneInfo("UTC"), set())
        self.assertTrue(any("aucun clip" in a for a in alerts))

    def test_clip_recent_au_premier_passage_n_alerte_pas(self):
        now = dt.datetime.now(ZoneInfo("UTC"))
        iso = (now - dt.timedelta(hours=1)).isoformat()
        current = {"cameras": self._cameras(), "modules": [],
                  "last_clip": {"jardin": iso}}
        alerts, _ = watch.compare({}, current, ZoneInfo("UTC"), set())
        self.assertFalse(any("aucun clip" in a for a in alerts))

    def test_silence_deja_signale_ne_repete_pas(self):
        now = dt.datetime.now(ZoneInfo("UTC"))
        iso = (now - dt.timedelta(days=10)).isoformat()
        cameras = self._cameras()
        previous = {"at": (now - dt.timedelta(days=5)).isoformat(),
                    "cameras": cameras, "modules": [], "last_clip": {"jardin": iso}}
        current = {"cameras": cameras, "modules": [], "last_clip": {"jardin": iso}}
        alerts, _ = watch.compare(previous, current, ZoneInfo("UTC"), set())
        self.assertFalse(any("aucun clip" in a for a in alerts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
