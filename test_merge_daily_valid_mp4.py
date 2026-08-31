"""Validation MP4 rapide pour les listings, approfondie pour l'ingestion."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import merge_daily


def boite(nom: bytes, contenu: bytes = b"") -> bytes:
    return (len(contenu) + 8).to_bytes(4, "big") + nom + contenu


FTYP = boite(b"ftyp", b"isom\x00\x00\x02\x00isomiso2")
MP4_STRUCTUREL = FTYP + boite(b"moov") + boite(b"mdat", b"paquet-video")
FMP4_STRUCTUREL = (
    FTYP + boite(b"moov") + boite(b"moof", b"fragment")
    + boite(b"mdat", b"paquet-fragmente")
)


class TestsValidMp4(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_valid_mp4_")
        self.racine = Path(self.temporaire.name)

    def tearDown(self) -> None:
        self.temporaire.cleanup()

    def test_mp4_classique_est_reconnu_structurellement(self):
        chemin = self.racine / "video.mp4"
        chemin.write_bytes(MP4_STRUCTUREL)
        self.assertTrue(merge_daily.valid_mp4(chemin))

    def test_mp4_fragmente_est_reconnu_structurellement(self):
        chemin = self.racine / "video-fragmente.mp4"
        chemin.write_bytes(FMP4_STRUCTUREL)
        self.assertTrue(merge_daily.valid_mp4(chemin))

    def test_ftyp_seul_meme_long_n_est_jamais_valide(self):
        chemin = self.racine / "tronque.mp4"
        chemin.write_bytes(FTYP + b"\x00" * 100)
        self.assertFalse(merge_daily.valid_mp4(chemin))

    def test_boite_declaree_plus_longue_que_le_fichier_est_rejetee(self):
        chemin = self.racine / "mdat-tronque.mp4"
        chemin.write_bytes(
            FTYP + boite(b"moov") + (4096).to_bytes(4, "big") + b"mdat" + b"court",
        )
        self.assertFalse(merge_daily.valid_mp4(chemin))

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
        chemin.write_bytes(MP4_STRUCTUREL)
        with mock.patch.object(Path, "read_bytes") as lecture_totale:
            self.assertTrue(merge_daily.valid_mp4(chemin))
        lecture_totale.assert_not_called()

    def test_listing_structurel_ne_lance_aucun_sous_processus(self):
        chemin = self.racine / "video.mp4"
        chemin.write_bytes(FMP4_STRUCTUREL)
        with mock.patch.object(merge_daily.runtime, "lancer") as lancer:
            self.assertTrue(merge_daily.valid_mp4(chemin))
        lancer.assert_not_called()

    def test_validation_complete_exige_une_sonde_sans_erreur(self):
        chemin = self.racine / "video.mp4"
        chemin.write_bytes(MP4_STRUCTUREL)
        succes = SimpleNamespace(returncode=0, stdout="3\n", stderr="")
        with mock.patch.object(
            merge_daily, "_outil_validation_media", return_value=("ffprobe", "ffprobe"),
        ), mock.patch.object(merge_daily.runtime, "lancer", return_value=succes) as lancer:
            self.assertTrue(merge_daily.valid_mp4_complet(chemin))
        self.assertIn("-count_packets", lancer.call_args.args[0])

        tronque = SimpleNamespace(returncode=1, stdout="", stderr="partial file")
        with mock.patch.object(
            merge_daily, "_outil_validation_media", return_value=("ffprobe", "ffprobe"),
        ), mock.patch.object(merge_daily.runtime, "lancer", return_value=tronque):
            self.assertFalse(merge_daily.valid_mp4_complet(chemin))


if __name__ == "__main__":
    unittest.main()
