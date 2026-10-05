"""Backlog « journalières sans distinction si deux caméras se nettoient pareil » :
« Garage » et « Garage! » donnent le même dossier par safe_name, et leurs
journalières s'écrivaient au même chemin, l'une écrasant l'autre. Elles
reçoivent maintenant des clés distinctes, comme les homonymes de deux réseaux
(test_merge_daily_homonymes.py), sans toucher aux caméras qui n'entrent pas en
collision."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8, édition Windows 7
    from backports.zoneinfo import ZoneInfo

import merge_daily
from test_merge_daily_homonymes import mp4_structurel


class NomsQuiSeNettoientPareil(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.input_dir = Path(self.tmp.name)
        self.tz = ZoneInfo("Europe/Paris")

    def _registre(self, cameras, reseau=""):
        clips = {}
        for numero, camera in enumerate(cameras):
            chemin = f"c{numero}.mp4"
            (self.input_dir / chemin).write_bytes(mp4_structurel())
            clips[f"k{numero}"] = {
                "camera": camera, "network_id": reseau, "device_id": "",
                "created_at": f"2026-09-01T1{numero}:00:00+00:00", "path": chemin,
            }
        (self.input_dir / merge_daily.DOWNLOAD_STATE).write_text(
            json.dumps({"version": 2, "clips": clips}), encoding="utf-8")

    def _cles(self):
        groupes = merge_daily.load_groups(self.input_dir, self.tz)
        return {camera for camera, _ in groupes}

    def _dossiers_distincts(self, cles):
        self.assertEqual(len({merge_daily.safe_name(c).casefold() for c in cles}), len(cles), cles)

    def test_garage_et_garage_point_d_exclamation_ne_partagent_plus_un_dossier(self):
        self._registre(["Garage", "Garage!"])
        cles = self._cles()
        self.assertEqual(len(cles), 2)
        self._dossiers_distincts(cles)
        # Le nom déjà propre garde son dossier : l'historique déjà assemblé reste.
        self.assertIn("Garage", cles)

    def test_le_choix_ne_depend_pas_de_l_ordre_du_registre(self):
        self._registre(["Garage!", "Garage"])
        premiere = self._cles()
        self._registre(["Garage", "Garage!"])
        self.assertEqual(premiere, self._cles())

    def test_trois_noms_qui_se_nettoient_pareil(self):
        self._registre(["Garage", "Garage!", "Garage?"])
        cles = self._cles()
        self.assertEqual(len(cles), 3)
        self._dossiers_distincts(cles)
        self.assertIn("Garage", cles)

    def test_la_casse_seule_distingue_deux_dossiers(self):
        # Windows et macOS confondent « Salon » et « salon » : même dossier.
        self._registre(["Salon", "salon"])
        cles = self._cles()
        self.assertEqual(len(cles), 2)
        self._dossiers_distincts(cles)

    def test_un_suffixe_qui_existe_deja_n_est_pas_reutilise(self):
        self._registre(["Garage", "Garage!", "Garage! (2)"])
        cles = self._cles()
        self.assertEqual(len(cles), 3)
        self._dossiers_distincts(cles)

    def test_noms_sans_collision_inchanges(self):
        self._registre(["Salon", "jardin", "Bureau", "Portail", "Terrasse1"])
        self.assertEqual(self._cles(), {"Salon", "jardin", "Bureau", "Portail", "Terrasse1"})

    def test_meme_nom_a_espace_final_reste_une_seule_camera(self):
        # La forme réelle de production : « jardin » et « jardin » + espace.
        self._registre(["jardin", "jardin "])
        self.assertEqual(self._cles(), {"jardin"})

    def test_deux_reseaux_et_un_nom_voisin_restent_distincts(self):
        entrees = {
            "a": {"camera": "jardin", "network_id": "1", "created_at": "2026-09-01T10:00:00+00:00"},
            "b": {"camera": "jardin", "network_id": "2", "created_at": "2026-09-01T11:00:00+00:00"},
            "c": {"camera": "jardin!", "network_id": "1", "created_at": "2026-09-01T12:00:00+00:00"},
        }
        cles = merge_daily._cles_camera_par_collision(entrees)
        finales = {merge_daily._camera_key(cles, e, e["camera"]) for e in entrees.values()}
        self.assertEqual(len(finales), 3, finales)
        self._dossiers_distincts(finales)

    def test_les_clips_se_repartissent_entre_les_deux_cles(self):
        self._registre(["Garage", "Garage!"])
        groupes = merge_daily.load_groups(self.input_dir, self.tz)
        self.assertEqual(sorted(len(v) for v in groupes.values()), [1, 1])

    def test_les_journees_indisponibles_utilisent_les_memes_cles(self):
        self._registre(["Garage", "Garage!"])
        (self.input_dir / "c0.mp4").unlink()
        (self.input_dir / "c1.mp4").unlink()
        jours, _ = merge_daily.journees_a_source_indisponible(self.input_dir, self.tz)
        cles_groupes = self._cles()
        self.assertEqual({camera for camera, _ in jours}, cles_groupes)


if __name__ == "__main__":
    unittest.main()
