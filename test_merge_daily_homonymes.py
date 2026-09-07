"""Bug rapporté le 2026-09-06 : deux caméras portant le même nom sur deux
hubs distincts finissaient dans le même assemblage journalier, le
regroupement ne reposant que sur le nom (merge_daily.py, load_groups()).

Le fix littéral (regrouper par identité stable partout) fragmenterait à
tort le registre réel de production : une même caméra physique y a
plusieurs (network_id, device_id) incohérents pour des raisons historiques
(vieilles entrées USB), avec toujours le même network_id quand il est
connu. Les tests ci-dessous couvrent donc les deux côtés : séparation
seulement sur une vraie collision de network_id, aucun changement sinon."""

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


def mp4_structurel() -> bytes:
    def boite(nom, contenu=b""):
        return (len(contenu) + 8).to_bytes(4, "big") + nom + contenu
    return (
        boite(b"ftyp", b"isom\x00\x00\x02\x00isom")
        + boite(b"moov") + boite(b"mdat", b"video")
    )


class TestsHomonymes(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.input_dir = Path(self.tmp.name)
        self.tz = ZoneInfo("Europe/Paris")

    def _ecrire_registre(self, clips: dict) -> None:
        chemin = self.input_dir / merge_daily.DOWNLOAD_STATE
        chemin.write_text(json.dumps({"version": 2, "clips": clips}), encoding="utf-8")

    def _clip(self, chemin: str) -> Path:
        source = self.input_dir / chemin
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(mp4_structurel())
        return source

    def test_deux_reseaux_distincts_ne_sont_plus_fusionnes(self):
        self._clip("a.mp4")
        self._clip("b.mp4")
        self._ecrire_registre({
            "a": {"camera": "jardin", "network_id": "111", "device_id": "",
                  "created_at": "2026-09-01T10:00:00+00:00", "path": "a.mp4"},
            "b": {"camera": "jardin", "network_id": "222", "device_id": "",
                  "created_at": "2026-09-01T11:00:00+00:00", "path": "b.mp4"},
        })
        groupes = merge_daily.load_groups(self.input_dir, self.tz)
        cameras_jardin = {camera for camera, _ in groupes if camera.startswith("jardin")}
        self.assertEqual(cameras_jardin, {"jardin", "jardin (2)"})
        for camera, _ in groupes:
            if camera.startswith("jardin"):
                self.assertEqual(len(groupes[(camera, "2026-09-01")]), 1)

    def test_reseau_inconnu_ou_repete_ne_fragmente_pas(self):
        """Reproduit la forme réelle de jardin/Terrasse1 en production : un
        seul network_id connu (toujours le même), le reste vide - un bug de
        clé incohérente déjà traité ailleurs, pas un homonyme."""
        self._clip("a.mp4")
        self._clip("b.mp4")
        self._clip("c.mp4")
        self._ecrire_registre({
            "a": {"camera": "jardin", "network_id": "436363", "device_id": "",
                  "created_at": "2026-09-01T09:00:00+00:00", "path": "a.mp4"},
            "b": {"camera": "jardin", "network_id": "436363", "device_id": "999",
                  "created_at": "2026-09-01T10:00:00+00:00", "path": "b.mp4"},
            "c": {"camera": "jardin", "network_id": "", "device_id": "",
                  "created_at": "2026-09-01T11:00:00+00:00", "path": "c.mp4"},
        })
        groupes = merge_daily.load_groups(self.input_dir, self.tz)
        self.assertEqual({camera for camera, _ in groupes}, {"jardin"})
        self.assertEqual(len(groupes[("jardin", "2026-09-01")]), 3)

    def test_identites_a_source_indisponible_suivent_la_meme_resolution(self):
        """journees_a_source_indisponible() doit produire les mêmes clés que
        load_groups() : sinon un clip manquant du réseau minoritaire
        exempterait à tort la journée du réseau principal (ou l'inverse)."""
        self._clip("a.mp4")
        self._ecrire_registre({
            "a": {"camera": "jardin", "network_id": "111", "device_id": "",
                  "created_at": "2026-09-01T10:00:00+00:00", "path": "a.mp4"},
            "b": {"camera": "jardin", "network_id": "222", "device_id": "",
                  "created_at": "2026-09-01T11:00:00+00:00", "path": "manquant.mp4"},
        })
        jours, _ = merge_daily.journees_a_source_indisponible(self.input_dir, self.tz)
        self.assertEqual(jours, {("jardin (2)", "2026-09-01")})
        groupes = merge_daily.load_groups(self.input_dir, self.tz)
        self.assertEqual(groupes[("jardin", "2026-09-01")], [
            (dt.datetime(2026, 9, 1, 10, tzinfo=dt.timezone.utc), (self.input_dir / "a.mp4").resolve()),
        ])
        self.assertEqual(groupes[("jardin (2)", "2026-09-01")], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
