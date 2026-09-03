"""Non-regression du cache de duree pour collect_videos (ASSEMBLED_DURATIONS) :
sans lui, chaque redemarrage du serveur relancait ffmpeg -i pour toutes les
videos journalieres/hebdomadaires/mensuelles existantes des la premiere
ouverture de l'onglet Clips - le cache memoire de probe_duration
(_DURATIONS, serve.py) ne survit pas a un redemarrage. Meme mecanisme que
EXCLUDED_DURATIONS pour les clips ecartes (test_serve_clips_window.py),
audit general demande par l'utilisateur, 2026-09-03."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-assembled-durations-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import serve  # noqa: E402 - bootstrap neutralise avant import


def boite(nom: bytes, contenu: bytes = b"") -> bytes:
    return (len(contenu) + 8).to_bytes(4, "big") + nom + contenu


# Meme structure minimale que test_merge_daily_valid_mp4.py (merge_daily.
# valid_mp4 == serve.md.valid_mp4, meme fonction, "import merge_daily as md").
FTYP = boite(b"ftyp", b"isom\x00\x00\x02\x00isomiso2")
MP4_STRUCTUREL = FTYP + boite(b"moov") + boite(b"mdat", b"paquet-video")


class CacheDureeVideosAssembleesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_assembled_test_")
        self.racine = Path(self.temporaire.name)
        self.paths = {
            "daily": self.racine / "daily",
            "weekly": self.racine / "weekly",
            "monthly": self.racine / "monthly",
            "thumbs": self.racine / "thumbs",
        }
        for chemin in self.paths.values():
            chemin.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temporaire.cleanup()

    def _ecrire_video(self, kind: str, camera: str, nom: str) -> Path:
        dossier = self.paths[kind] / camera
        dossier.mkdir(parents=True, exist_ok=True)
        chemin = dossier / nom
        chemin.write_bytes(MP4_STRUCTUREL)
        return chemin

    def test_duree_d_une_video_n_est_sondee_qu_une_fois(self) -> None:
        self._ecrire_video("daily", "jardin", "2026-08-15_jardin.mp4")

        appels = []

        def faux_probe(ffmpeg, source):
            appels.append(source)
            return 42.0

        with mock.patch.object(serve, "probe_duration", side_effect=faux_probe):
            premier = serve.collect_videos(self.paths, "ffmpeg")
            second = serve.collect_videos(self.paths, "ffmpeg")

        self.assertEqual(len(appels), 1, "ffmpeg -i n'aurait du etre relance qu'une fois")
        self.assertEqual(premier["daily"][0]["duration"], 42.0)
        self.assertEqual(second["daily"][0]["duration"], 42.0)
        self.assertTrue((self.paths["thumbs"] / serve.ASSEMBLED_DURATIONS).is_file())

    def test_video_remplacee_est_re_sondee(self) -> None:
        chemin = self._ecrire_video("daily", "jardin", "2026-08-15_jardin.mp4")

        appels = []

        def faux_probe(ffmpeg, source):
            appels.append(source)
            return float(len(appels))

        with mock.patch.object(serve, "probe_duration", side_effect=faux_probe):
            serve.collect_videos(self.paths, "ffmpeg")
            chemin.write_bytes(MP4_STRUCTUREL + b"\x00" * 16)  # taille differente
            resultat = serve.collect_videos(self.paths, "ffmpeg")

        self.assertEqual(len(appels), 2, "une video remplacee doit etre re-sondee")
        self.assertEqual(resultat["daily"][0]["duration"], 2.0)

    def test_meme_nom_dans_des_periodes_differentes_ne_se_confond_pas(self) -> None:
        # daily/jardin/x.mp4 et weekly/jardin/x.mp4 partagent le meme chemin
        # relatif (camera/nom) mais sont deux fichiers distincts : l'identite
        # de cache doit inclure la periode (kind), pas seulement camera/nom.
        self._ecrire_video("daily", "jardin", "x.mp4")
        self._ecrire_video("weekly", "jardin", "x.mp4")

        appels = []

        def faux_probe(ffmpeg, source):
            appels.append(source)
            return float(len(appels))

        with mock.patch.object(serve, "probe_duration", side_effect=faux_probe):
            resultat = serve.collect_videos(self.paths, "ffmpeg")

        self.assertEqual(len(appels), 2, "deux fichiers distincts doivent etre sondes chacun")
        self.assertEqual(resultat["daily"][0]["duration"], 1.0)
        self.assertEqual(resultat["weekly"][0]["duration"], 2.0)


if __name__ == "__main__":
    unittest.main()
