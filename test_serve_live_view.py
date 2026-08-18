"""Non-régression du direct MSE : segment d'initialisation MP4 (AUDIT-2026-08-13.md,
section 28.22) et défaut d'accumulation corrigé le 18 août 2026."""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-live-view-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import serve  # noqa: E402 - bootstrap neutralisé avant import


def _boite(genre: bytes, charge: bytes) -> bytes:
    return (8 + len(charge)).to_bytes(4, "big") + genre + charge


def _segment_synthetique() -> bytes:
    """ftyp + moov > trak > mdia > minf > stbl > stsd > avc1 > avcC, avec
    profil/contraintes/niveau H.264 High (avc1.640028, vu en vrai en
    production)."""
    avcc = _boite(b"avcC", bytes([1, 0x64, 0x00, 0x28]) + bytes(16))
    avc1 = _boite(b"avc1", bytes(78) + avcc)
    stsd = _boite(b"stsd", bytes(8) + avc1)
    stbl = _boite(b"stbl", stsd)
    minf = _boite(b"minf", stbl)
    mdia = _boite(b"mdia", minf)
    trak = _boite(b"trak", mdia)
    moov = _boite(b"moov", trak)
    ftyp = _boite(b"ftyp", b"isom" + bytes(4))
    return ftyp + moov


class FauxPipeMorceaux:
    """Un tube qui ne rend ses octets qu'au compte-gouttes, comme ffmpeg le
    fait pour un moov qui déborde du premier bloc lu."""

    def __init__(self, morceaux: list):
        self._morceaux = list(morceaux)

    def read(self, _n: int) -> bytes:
        return self._morceaux.pop(0) if self._morceaux else b""


class TestsInitSegmentMse(unittest.TestCase):
    def test_moov_tenant_dans_un_seul_bloc(self):
        """Cas simple, déjà correct avant le correctif : un seul appel suffit."""
        segment = _segment_synthetique()
        pipe = FauxPipeMorceaux([segment])
        resultat = serve.read_mp4_init_segment(pipe, seconds=2.0)
        self.assertEqual(resultat, segment)
        self.assertEqual(serve.h264_mime_codec_from_moov(resultat), "avc1.640028")

    def test_moov_etale_sur_deux_blocs_est_reassemble(self):
        """Bug corrigé le 18 août 2026 : `read_with_deadline` appelé en
        boucle lançait un nouveau fil à chaque tour sans jamais nettoyer le
        précédent, laissant un fil orphelin bloqué sur le même tube dès
        qu'un premier appel n'aboutissait pas dans son délai. Un second
        appel concurrent sur le même tube pouvait alors perdre les octets
        suivants au profit de ce fil orphelin, et la fonction abandonnait
        avec un tampon incomplet — reproduit en vrai sur une caméra plus
        lente à répondre (moov étalé sur plusieurs blocs), jamais sur une
        caméra qui répond vite (un seul bloc a toujours suffi)."""
        segment = _segment_synthetique()
        coupure = 100  # tombe au milieu de trak : le premier bloc seul est incomplet
        pipe = FauxPipeMorceaux([segment[:coupure], segment[coupure:]])

        resultat = serve.read_mp4_init_segment(pipe, seconds=2.0)

        self.assertEqual(resultat, segment)
        self.assertEqual(serve.h264_mime_codec_from_moov(resultat), "avc1.640028")

    def test_moov_incomplet_a_la_fin_du_flux_renvoie_ce_qui_est_arrive(self):
        """Le tube se ferme (EOF) avant un avcC complet : pas d'exception,
        juste ce qui a pu être accumulé — c'est à l'appelant de traiter un
        résultat vide ou incomplet comme un échec de direct."""
        segment = _segment_synthetique()
        pipe = FauxPipeMorceaux([segment[:100], b""])
        resultat = serve.read_mp4_init_segment(pipe, seconds=2.0)
        self.assertEqual(resultat, segment[:100])


if __name__ == "__main__":
    unittest.main()
