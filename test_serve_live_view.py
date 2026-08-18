"""Non-régression du direct MSE/MJPEG : lecture de tube avec un vrai délai
(AUDIT-2026-08-13.md, sections 28.22 et 28.26)."""

from __future__ import annotations

import os
import tempfile
import time
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


class FauxPipeLent:
    """Bloque plus longtemps que le délai testé avant de rendre un octet,
    comme ffmpeg qui se tait un moment sans jamais fermer son tube."""

    def __init__(self, attente: float, morceau: bytes):
        self._attente = attente
        self._morceau = morceau
        self._rendu = False

    def read(self, _n: int) -> bytes:
        if self._rendu:
            return b""
        time.sleep(self._attente)
        self._rendu = True
        return self._morceau


class TestsInitSegmentMse(unittest.TestCase):
    def test_moov_tenant_dans_un_seul_bloc(self):
        """Cas simple, déjà correct avant le correctif : un seul appel suffit."""
        segment = _segment_synthetique()
        lecteur = serve.LecteurTube(FauxPipeMorceaux([segment]))
        resultat = serve.read_mp4_init_segment(lecteur, seconds=2.0)
        self.assertEqual(resultat, segment)
        self.assertEqual(serve.h264_mime_codec_from_moov(resultat), "avc1.640028")

    def test_moov_etale_sur_deux_blocs_est_reassemble(self):
        """Bug corrigé le 18 août 2026 (28.22) : lancer un fil par appel sur
        le même tube laissait un fil orphelin bloqué dès qu'un appel
        n'aboutissait pas dans son délai ; un second appel concurrent sur le
        même tube pouvait perdre les octets suivants à son profit. Reproduit
        en vrai sur une caméra plus lente à répondre (moov étalé sur
        plusieurs blocs), jamais sur une caméra qui répond vite."""
        segment = _segment_synthetique()
        coupure = 100  # tombe au milieu de trak : le premier bloc seul est incomplet
        lecteur = serve.LecteurTube(
            FauxPipeMorceaux([segment[:coupure], segment[coupure:]])
        )

        resultat = serve.read_mp4_init_segment(lecteur, seconds=2.0)

        self.assertEqual(resultat, segment)
        self.assertEqual(serve.h264_mime_codec_from_moov(resultat), "avc1.640028")

    def test_moov_incomplet_a_la_fin_du_flux_renvoie_ce_qui_est_arrive(self):
        """Le tube se ferme (EOF) avant un avcC complet : pas d'exception,
        juste ce qui a pu être accumulé — c'est à l'appelant de traiter un
        résultat vide ou incomplet comme un échec de direct."""
        segment = _segment_synthetique()
        lecteur = serve.LecteurTube(FauxPipeMorceaux([segment[:100], b""]))
        resultat = serve.read_mp4_init_segment(lecteur, seconds=2.0)
        self.assertEqual(resultat, segment[:100])


class TestsLecteurTube(unittest.TestCase):
    def test_lire_rend_none_sur_delai_sans_perdre_la_donnee_qui_suit(self):
        """Bug corrigé le 18 août 2026 (28.26) : la boucle d'envoi principale
        de /live et /live-mse lisait le tube en direct
        (process.stdout.read(16384)), sans aucun délai réel — seule la
        condition ENTRE deux lectures regardait LIVE_MAX_SECONDS, ce qui ne
        bornait rien si une lecture individuelle bloquait plus longtemps
        (vu en vrai : un direct resté ouvert plus de 600 s, MODULE_SLOT
        jamais rendu, alors que LIVE_MAX_SECONDS vaut 300). ``lire(délai)``
        doit rendre ``None`` (pas ``b""``, qui signifierait EOF) dès que le
        délai est écoulé, sans jamais perdre la donnée qui arrive ensuite."""
        pipe = FauxPipeLent(attente=0.3, morceau=b"donnees")
        lecteur = serve.LecteurTube(pipe)

        premier = lecteur.lire(0.05)
        self.assertIsNone(premier)

        second = lecteur.lire(1.0)
        self.assertEqual(second, b"donnees")

    def test_lire_rend_bytes_vides_sur_vraie_fin_de_tube(self):
        """EOF réel (tube fermé) distinct d'un simple délai écoulé : ``b""``,
        jamais ``None``, sans quoi l'appelant boucle indéfiniment sur un
        flux qui ne reviendra plus."""
        lecteur = serve.LecteurTube(FauxPipeMorceaux([b""]))
        self.assertEqual(lecteur.lire(1.0), b"")


if __name__ == "__main__":
    unittest.main()
