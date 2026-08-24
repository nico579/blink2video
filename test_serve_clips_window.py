"""Non-régression du plafonnement et du cache de durée de /api/clips.

Deux défauts corrigés hors séquence le 17 août 2026 (voir AUDIT-2026-08-13.md,
section 28.6) : l'inventaire grossissait indéfiniment sans fenêtre par défaut,
et la durée d'un clip écarté était re-sondée par ffmpeg à chaque requête."""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8, édition Windows 7
    from backports.zoneinfo import ZoneInfo

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-window-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import serve  # noqa: E402 - bootstrap neutralisé avant import


class BacASable(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_window_test_")
        self.racine = Path(self.temporaire.name)
        self.paths = {
            "input": self.racine / "clips",
            "normalized": self.racine / "normalized",
            "excluded": self.racine / "excluded",
            "thumbs": self.racine / "thumbs",
        }
        for chemin in self.paths.values():
            chemin.mkdir(parents=True, exist_ok=True)
        self.timezone = ZoneInfo("UTC")

    def tearDown(self) -> None:
        self.temporaire.cleanup()

    def ecrire_registre(self, entries: dict) -> None:
        (self.paths["input"] / serve.md.DOWNLOAD_STATE).write_text(
            json.dumps({"clips": entries}), encoding="utf-8"
        )

    def entree(self, identity: str, created: str, excluded: bool = False) -> dict:
        return {
            "path": identity, "created_at": created, "camera": "jardin",
            "excluded": excluded, "source": "usb",
        }


class FenetreParDefautTests(BacASable):
    def test_clip_recent_visible_clip_ancien_masque_par_defaut(self) -> None:
        recent = self.entree("recent.mp4", "2026-08-15T12:00:00+00:00")
        ancien = self.entree("ancien.mp4", "2026-01-01T12:00:00+00:00")
        self.ecrire_registre({"recent.mp4": recent, "ancien.mp4": ancien})

        cutoff = dt.date(2026, 8, 17) - dt.timedelta(days=30)
        resultat = serve.collect(self.paths, self.timezone, depuis=cutoff)

        identites = {c["identity"] for c in resultat["clips"]}
        self.assertEqual(identites, {"recent.mp4"})
        self.assertEqual(resultat["total_known"], 2)
        self.assertEqual(resultat["window_days"], serve.DEFAULT_WINDOW_DAYS)

    def test_depuis_none_renvoie_tout_l_historique(self) -> None:
        recent = self.entree("recent.mp4", "2026-08-15T12:00:00+00:00")
        ancien = self.entree("ancien.mp4", "2026-01-01T12:00:00+00:00")
        self.ecrire_registre({"recent.mp4": recent, "ancien.mp4": ancien})

        resultat = serve.collect(self.paths, self.timezone, depuis=None)

        identites = {c["identity"] for c in resultat["clips"]}
        self.assertEqual(identites, {"recent.mp4", "ancien.mp4"})
        self.assertIsNone(resultat["window_days"])


class CacheDureeTests(BacASable):
    def test_duree_d_un_clip_ecarte_n_est_sondee_qu_une_fois(self) -> None:
        identity = "jardin/ecarte.mp4"
        media = self.paths["excluded"] / identity
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"\x00" * 16)
        self.ecrire_registre({
            identity: self.entree(identity, "2026-08-15T12:00:00+00:00", excluded=True),
        })

        appels = []

        def faux_probe(ffmpeg, source):
            appels.append(source)
            return 12.5

        with mock.patch.object(serve, "probe_duration", side_effect=faux_probe):
            premier = serve.collect(self.paths, self.timezone, ffmpeg="ffmpeg")
            second = serve.collect(self.paths, self.timezone, ffmpeg="ffmpeg")

        self.assertEqual(len(appels), 1, "ffmpeg -i n'aurait dû être relancé qu'une fois")
        self.assertEqual(premier["clips"][0]["duration"], 12.5)
        self.assertEqual(second["clips"][0]["duration"], 12.5)
        self.assertTrue((self.paths["thumbs"] / serve.EXCLUDED_DURATIONS).is_file())

    def test_fichier_remplace_est_re_sonde(self) -> None:
        identity = "jardin/ecarte.mp4"
        media = self.paths["excluded"] / identity
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"\x00" * 16)
        self.ecrire_registre({
            identity: self.entree(identity, "2026-08-15T12:00:00+00:00", excluded=True),
        })

        appels = []

        def faux_probe(ffmpeg, source):
            appels.append(source)
            return float(len(appels))

        with mock.patch.object(serve, "probe_duration", side_effect=faux_probe):
            serve.collect(self.paths, self.timezone, ffmpeg="ffmpeg")
            media.write_bytes(b"\x00" * 32)  # taille différente : empreinte changée
            resultat = serve.collect(self.paths, self.timezone, ffmpeg="ffmpeg")

        self.assertEqual(len(appels), 2, "un média remplacé doit être re-sondé")
        self.assertEqual(resultat["clips"][0]["duration"], 2.0)


if __name__ == "__main__":
    unittest.main()
