"""Horloge 12 h ou 24 h de l'horodatage incrusté (demande Reddit, 2026-11).

Le 24 h doit rester exactement l'ancien rendu : même filtre, même empreinte de
segment, même ligne de commande. Sinon la mise à jour réencoderait tous les
segments déjà normalisés pour une image identique. Le rendu réel en 12 h
(pixels de « PM ») est éprouvé par tests.py, sur le bundle de chaque OS."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import merge_daily as md
import runtime


class TestsFiltreHorloge(unittest.TestCase):
    def test_24h_garde_exactement_l_ancien_texte(self):
        graphe = md.drawtext_chain("'police.ttf'", 40, 7)
        self.assertIn(r"text='%{pts\:localtime\:7\:%d/%m/%Y %X}'", graphe)
        self.assertEqual(graphe, md.drawtext_chain("'police.ttf'", 40, 7, clock="24h"))

    def test_12h_echappe_les_deux_points_trois_fois(self):
        graphe = md.drawtext_chain("'police.ttf'", 40, 7, clock="12h")
        self.assertIn(r"%d/%m/%Y %I\\\:%M\\\:%S %p}'", graphe)
        # %r rend une chaîne vide avec le strftime de Windows.
        self.assertNotIn("%r", graphe)

    def test_horloge_inconnue_retombe_sur_24h(self):
        self.assertEqual(md.drawtext_chain("'p.ttf'", 40, 7, clock="?"),
                         md.drawtext_chain("'p.ttf'", 40, 7))

    def test_le_style_porte_l_horloge_jusqu_au_filtre_du_lot(self):
        clip = md.ClipInfo(
            created=dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc),
            source=Path("brut.mp4"), duration=5.0, width=1920, height=1080,
            fps=30.0, has_audio=True,
        )
        style = md.TimestampStyle(None, "white", 0.55, "12h")
        graphe = md.build_batch_filter(
            [clip], 1920, 1080, 30.0, md.ZoneInfo("UTC"), "'police.ttf'", style)
        self.assertIn("%p}'", graphe)


class TestsEmpreinteHorloge(unittest.TestCase):
    def setUp(self):
        temporaire = tempfile.TemporaryDirectory(prefix="blink_horloge_cle_")
        self.addCleanup(temporaire.cleanup)
        source = Path(temporaire.name) / "brut.mp4"
        source.write_bytes(b"\x00" * 16)
        self.clip = md.ClipInfo(
            created=dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc),
            source=source, duration=5.0, width=1920, height=1080,
            fps=30.0, has_audio=True,
        )

    def cle(self, font_path, style):
        return md.render_key("jardin/clip.mp4", self.clip, (1920, 1080, 30.0),
                             1755500000, font_path, "veryfast", 21, style)

    def test_24h_garde_l_empreinte_d_avant_le_reglage(self):
        ancien = md.TimestampStyle(None, "white", 0.55)
        self.assertEqual(ancien.clock, "24h")
        self.assertEqual(
            self.cle(Path("p.ttf"), ancien),
            self.cle(Path("p.ttf"), md.TimestampStyle(None, "white", 0.55, "24h")))

    def test_12h_force_le_reencodage(self):
        self.assertNotEqual(
            self.cle(Path("p.ttf"), md.TimestampStyle(None, "white", 0.55, "24h")),
            self.cle(Path("p.ttf"), md.TimestampStyle(None, "white", 0.55, "12h")))

    def test_horloge_ignoree_sans_horodatage(self):
        self.assertEqual(
            self.cle(None, md.TimestampStyle(None, "white", 0.55, "24h")),
            self.cle(None, md.TimestampStyle(None, "white", 0.55, "12h")))


class TestsReglageHorloge(unittest.TestCase):
    def setUp(self):
        temporaire = tempfile.TemporaryDirectory(prefix="blink_horloge_reglage_")
        self.addCleanup(temporaire.cleanup)
        self.dossier = Path(temporaire.name)

    def test_options_de_fusion(self):
        reglages = dict(runtime.REGLAGES_DEFAUT, timestamp=True)
        self.assertNotIn("--clock", runtime.options_fusion(reglages))
        reglages["clock"] = "12h"
        options = runtime.options_fusion(reglages)
        self.assertEqual(options[options.index("--clock") + 1], "12h")
        # Sans horodatage, l'horloge n'a rien à dessiner.
        reglages["timestamp"] = False
        self.assertNotIn("--clock", runtime.options_fusion(reglages))

    def test_ecriture_puis_relecture(self):
        valeurs = dict(runtime.REGLAGES_DEFAUT, clock="12h")
        runtime.ecrire_reglages(**valeurs, dossier=self.dossier)
        with mock.patch.object(runtime, "app_dir", return_value=self.dossier):
            self.assertEqual(runtime.lire_reglages()["clock"], "12h")

    def test_valeur_inconnue_relue_en_24h(self):
        (self.dossier / runtime.REGLAGES).write_text(
            json.dumps({"clock": "13h"}), encoding="utf-8")
        with mock.patch.object(runtime, "app_dir", return_value=self.dossier):
            self.assertEqual(runtime.lire_reglages()["clock"], "24h")


if __name__ == "__main__":
    unittest.main()
