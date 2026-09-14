"""Style de l'horodatage incrusté (issue GitHub #7) : taille, couleur et
opacité du bandeau, configurables via --font-size/--font-color/--box-opacity
en plus du --font déjà existant. Couvre la validation argparse, le
filtergraph produit, et l'empreinte de rendu (render_key) dont dépend le
caractère incrémental de la fusion : un changement de style doit forcer un
réencodage, mais seulement quand l'horodatage est actif."""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
import tempfile
import unittest
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8, édition Windows 7
    from backports.zoneinfo import ZoneInfo

import merge_daily as md


class TestsValidationCouleur(unittest.TestCase):
    def test_noms_et_hexadecimaux_acceptes(self):
        for valeur in ("white", "yellow", "SkyBlue", "#FFAA00", "0x1a2b3c",
                       "white@0.5", "0x000000@1.0", "red@0"):
            with self.subTest(valeur=valeur):
                self.assertEqual(md._couleur_ffmpeg_valide(valeur), valeur)

    def test_caracteres_de_filtergraph_refuses(self):
        for valeur in ("white:boxcolor=red", "white,drawbox", "white;rm -rf",
                       "white'", 'white"', "white@1.5", "white@-1"):
            with self.subTest(valeur=valeur):
                with self.assertRaises(argparse.ArgumentTypeError):
                    md._couleur_ffmpeg_valide(valeur)


class TestsValidationTailleEtOpacite(unittest.TestCase):
    def test_taille_dans_les_bornes_acceptee(self):
        self.assertEqual(md._taille_police_valide("40"), 40)
        self.assertEqual(md._taille_police_valide("8"), 8)
        self.assertEqual(md._taille_police_valide("500"), 500)

    def test_taille_hors_bornes_ou_non_numerique_refusee(self):
        for valeur in ("7", "501", "0", "-5", "abc", ""):
            with self.subTest(valeur=valeur):
                with self.assertRaises(argparse.ArgumentTypeError):
                    md._taille_police_valide(valeur)

    def test_opacite_dans_les_bornes_acceptee(self):
        self.assertEqual(md._opacite_valide("0"), 0.0)
        self.assertEqual(md._opacite_valide("0.55"), 0.55)
        self.assertEqual(md._opacite_valide("1"), 1.0)

    def test_opacite_hors_bornes_ou_non_numerique_refusee(self):
        for valeur in ("-0.1", "1.1", "abc", ""):
            with self.subTest(valeur=valeur):
                with self.assertRaises(argparse.ArgumentTypeError):
                    md._opacite_valide(valeur)


class TestsDrawtextChainStyle(unittest.TestCase):
    def test_couleur_et_opacite_par_defaut(self):
        graphe = md.drawtext_chain("'police.ttf'", 40, 0)
        self.assertIn("fontcolor=white:", graphe)
        self.assertIn("boxcolor=black@0.55:", graphe)

    def test_couleur_et_opacite_personnalisees(self):
        graphe = md.drawtext_chain("'police.ttf'", 40, 0, "yellow", 0.2)
        self.assertIn("fontcolor=yellow:", graphe)
        self.assertIn("boxcolor=black@0.2:", graphe)


class TestsBuildBatchFilterStyle(unittest.TestCase):
    def _clip(self):
        return md.ClipInfo(
            created=dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc),
            source=Path("brut.mp4"), duration=5.0, width=1920, height=1080,
            fps=30.0, has_audio=True,
        )

    def test_taille_automatique_par_defaut(self):
        graphe = md.build_batch_filter(
            [self._clip()], 1920, 1080, 30.0, ZoneInfo("UTC"), "'police.ttf'",
        )
        # max(18, 1080 // 18) == 60, le calcul historique, inchangé sans --font-size.
        self.assertIn("fontsize=60:", graphe)

    def test_taille_explicite_l_emporte_sur_le_calcul_automatique(self):
        style = md.TimestampStyle(size=96, color="white", box_opacity=0.55)
        graphe = md.build_batch_filter(
            [self._clip()], 1920, 1080, 30.0, ZoneInfo("UTC"), "'police.ttf'",
            style,
        )
        self.assertIn("fontsize=96:", graphe)


class TestsRenderKeyStyle(unittest.TestCase):
    def setUp(self):
        # render_key() lit la taille/date du fichier (stat_tag) : il doit
        # réellement exister, comme dans TestsRenderKeyAvecEtSansHorodatage
        # (test_merge_daily_horodatage_optionnel.py).
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_style_key_")
        self.addCleanup(self.temporaire.cleanup)
        source = Path(self.temporaire.name) / "brut.mp4"
        source.write_bytes(b"\x00" * 16)
        self.clip = md.ClipInfo(
            created=dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc),
            source=source, duration=5.0, width=1920, height=1080,
            fps=30.0, has_audio=True,
        )
        self.police = Path("police.ttf")

    def cle(self, font_path, style=md.STYLE_PAR_DEFAUT):
        return md.render_key(
            "jardin/clip.mp4", self.clip, (1920, 1080, 30.0), 1755500000,
            font_path, "veryfast", 21, style,
        )

    def test_changer_le_style_change_l_empreinte_avec_horodatage(self):
        blanc = self.cle(self.police, md.TimestampStyle(None, "white", 0.55))
        jaune = self.cle(self.police, md.TimestampStyle(None, "yellow", 0.55))
        self.assertNotEqual(blanc, jaune,
                            "un clip deja normalise doit se re-encoder si la "
                            "couleur de l'horodatage change, pas rester tel quel")

    def test_meme_style_meme_empreinte(self):
        style = md.TimestampStyle(72, "red@0.5", 0.2)
        self.assertEqual(self.cle(self.police, style), self.cle(self.police, style))

    def test_le_style_est_ignore_sans_horodatage(self):
        # font_path=None : aucun texte ne sera jamais dessiné, changer la
        # couleur ne doit donc pas déclencher un réencodage inutile.
        sans_a = self.cle(None, md.TimestampStyle(None, "white", 0.55))
        sans_b = self.cle(None, md.TimestampStyle(96, "yellow", 0.1))
        self.assertEqual(sans_a, sans_b)


if __name__ == "__main__":
    unittest.main()
