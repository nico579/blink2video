"""Non-régression de valid_mp4 (AUDIT-2026-08-13.md, section 28.27) : lecture
bornée à l'en-tête, pas au fichier entier."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import merge_daily


class TestsValidMp4(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_valid_mp4_")
        self.racine = Path(self.temporaire.name)

    def tearDown(self) -> None:
        self.temporaire.cleanup()

    def test_fichier_avec_ftyp_est_valide(self):
        chemin = self.racine / "video.mp4"
        chemin.write_bytes(b"    ftyp" + b"\x00" * 56)
        self.assertTrue(merge_daily.valid_mp4(chemin))

    def test_fichier_sans_ftyp_n_est_pas_valide(self):
        chemin = self.racine / "video.mp4"
        chemin.write_bytes(b"\x00" * 64)
        self.assertFalse(merge_daily.valid_mp4(chemin))

    def test_fichier_vide_n_est_pas_valide(self):
        chemin = self.racine / "video.mp4"
        chemin.touch()
        self.assertFalse(merge_daily.valid_mp4(chemin))

    def test_fichier_absent_n_est_pas_valide(self):
        self.assertFalse(merge_daily.valid_mp4(self.racine / "absent.mp4"))

    def test_ne_lit_pas_le_fichier_entier(self):
        """Bug corrigé le 18 août 2026 : l'ancienne implémentation
        (``read_bytes()[:64]``) chargeait le fichier entier en mémoire avant
        de ne garder que les 64 premiers octets — invisible sur un clip,
        coûteux sur une vidéo assemblée de plusieurs centaines de Mo à
        quelques Go (vu en vrai : /api/videos passait de plus de 60 s à
        moins de 3 s sur les mêmes 19 fichiers après ce correctif, sans
        rapport avec ffmpeg — le sondage de durée, déjà en cache, n'était
        pas la cause). Vérifié ici en s'assurant qu'aucune lecture totale
        n'a lieu, quelle que soit la taille réelle du fichier de test."""
        chemin = self.racine / "video.mp4"
        chemin.write_bytes(b"    ftyp" + b"\x00" * 56)
        with mock.patch.object(Path, "read_bytes") as lecture_totale:
            self.assertTrue(merge_daily.valid_mp4(chemin))
        lecture_totale.assert_not_called()


if __name__ == "__main__":
    unittest.main()
