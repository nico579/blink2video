"""Issue #18 : les Blink Mini 2 / Mini 2K+ restaient sur « Réveil de la
caméra… » en WebRTC, alors que MSE (découpage par ffmpeg) les affichait.
_PisteH264 ne fermait une unité d'accès que sur un AUD (NAL 9), émis par les
Mini d'origine : sans AUD, aucune image complète n'atteignait le navigateur.
Le découpage suit désormais ITU-T H.264 §7.4.1.2.3."""

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path

import blink_ts_demux
import blink_webrtc
from test_webrtc_roundtrip import trouver_ffmpeg


def _nal(entete: int, charge: bytes = b"\x00") -> bytes:
    return b"\x00\x00\x00\x01" + bytes([entete]) + charge


AUD = _nal(0x09, b"\xf0")
SEI = _nal(0x06, b"\x05")
SPS = _nal(0x67, b"\x64\x00\x28")
PPS = _nal(0x68, b"\xee")
# Premier bit après l'en-tête = first_mb_in_slice ue(v) : 1 -> vaut 0.
IDR = _nal(0x65, b"\x88\x84")
IDR_SUITE = _nal(0x65, b"\x1a\x84")  # first_mb_in_slice > 0 : même image
P = _nal(0x41, b"\x9a\x02")
P_SUITE = _nal(0x41, b"\x12\x02")


def decouper_annexb(flux: bytes) -> list:
    """NAL units d'un flux Annexe B brut, start code de 4 octets remis."""
    positions = []
    i = flux.find(b"\x00\x00\x01")
    while i != -1:
        positions.append(i)
        i = flux.find(b"\x00\x00\x01", i + 3)
    nals = []
    for n, debut in enumerate(positions):
        fin = positions[n + 1] if n + 1 < len(positions) else len(flux)
        if fin < len(flux) and flux[fin - 1] == 0:
            fin -= 1
        nals.append(b"\x00\x00\x00\x01" + flux[debut + 3:fin])
    return nals


class TestsDebutUniteAcces(unittest.TestCase):
    def test_nal_non_vcl_ouvrant_une_unite(self):
        for nal in (AUD, SEI, SPS, PPS, _nal(0x0E), _nal(0x12)):
            with self.subTest(type_=blink_ts_demux.type_nal(nal)):
                self.assertTrue(blink_ts_demux.debut_unite_acces(nal))

    def test_tranche_selon_first_mb_in_slice(self):
        self.assertTrue(blink_ts_demux.debut_unite_acces(IDR))
        self.assertTrue(blink_ts_demux.debut_unite_acces(P))
        self.assertFalse(blink_ts_demux.debut_unite_acces(IDR_SUITE))
        self.assertFalse(blink_ts_demux.debut_unite_acces(P_SUITE))

    def test_start_code_trois_octets_et_nal_tronquee(self):
        self.assertTrue(blink_ts_demux.debut_unite_acces(b"\x00\x00\x01\x65\x88"))
        self.assertFalse(blink_ts_demux.debut_unite_acces(b"\x00\x00\x01\x65"))

    def test_autres_types_n_ouvrent_pas_d_unite(self):
        for entete in (0x0A, 0x0B, 0x0C, 0x13):  # fin de séquence/flux, remplissage, auxiliaire
            with self.subTest(entete=entete):
                self.assertFalse(blink_ts_demux.debut_unite_acces(_nal(entete)))


@unittest.skipUnless(blink_webrtc.DISPONIBLE, "aiortc optionnel non installé")
class TestsDecoupageSansAUD(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.track = blink_webrtc._PisteH264(asyncio.StreamReader())
        self.track._synchroniser_enregistrement = lambda _unite: None

    async def asyncTearDown(self):
        self.track.fermer()
        with self.assertRaises(asyncio.CancelledError):
            await self.track._tache

    def _unites(self) -> list:
        unites = []
        while not self.track._file.empty():
            item = self.track._file.get_nowait()
            if item is not None:
                unites.append(item)
        return unites

    def _alimenter(self, nals_pts) -> list:
        for pts, nal in nals_pts:
            self.track._traiter_nal(pts, nal)
        self.track._flush_unite_courante()
        return self._unites()

    async def test_flux_sans_aud_decoupe_par_image_multi_tranches(self):
        unites = self._alimenter([
            (0, SPS), (0, PPS), (0, IDR), (0, IDR_SUITE),
            (3000, P), (3000, P_SUITE),
            (6000, SEI), (6000, P),
        ])
        self.assertEqual(unites, [
            (0, SPS + PPS + IDR + IDR_SUITE),
            (3000, P + P_SUITE),
            (6000, SEI + P),
        ])
        self.assertEqual(self.track.sps_pps, SPS + PPS)
        self.assertTrue(self.track.sps_pps_pret.is_set())

    async def test_flux_avec_aud_inchange(self):
        unites = self._alimenter([
            (0, AUD), (0, SEI), (0, SPS), (0, PPS), (0, IDR), (0, IDR_SUITE),
            (3000, AUD), (3000, P), (3000, P_SUITE),
        ])
        self.assertEqual(unites, [
            (0, AUD + SEI + SPS + PPS + IDR + IDR_SUITE),
            (3000, AUD + P + P_SUITE),
        ])

    async def test_pts_de_l_unite_est_celui_de_sa_premiere_nal(self):
        unites = self._alimenter([(None, SPS), (0, PPS), (0, IDR), (3000, P)])
        self.assertEqual([pts for pts, _ in unites], [None, 3000])

    async def test_unite_jamais_close_est_plafonnee(self):
        enorme = _nal(0x65, b"\x1a" + bytes(1024))
        self.track._traiter_nal(0, IDR)
        with self.assertRaisesRegex(RuntimeError, "sans fin détectable"):
            for _ in range(blink_webrtc.FILE_IMAGES_MAX_OCTETS // len(enorme) + 2):
                self.track._traiter_nal(0, enorme)

    async def test_vrai_flux_libx264_sans_aud_decode_image_par_image(self):
        ffmpeg = trouver_ffmpeg()
        if ffmpeg is None:
            self.skipTest("ffmpeg non installé")
        images = 12
        with tempfile.TemporaryDirectory(prefix="blink-sans-aud-") as dossier:
            sortie = Path(dossier) / "flux.h264"
            # x264 n'émet pas d'AUD par défaut (aud=0) ; quatre tranches par
            # image vérifient qu'une image n'est pas coupée entre ses tranches.
            subprocess.run([
                ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10,format=yuv420p",
                "-frames:v", str(images), "-c:v", "libx264", "-profile:v", "high",
                "-x264-params", "aud=0:slices=4", "-bf", "0", "-f", "h264", str(sortie),
            ], check=True, timeout=60)
            nals = decouper_annexb(sortie.read_bytes())
        self.assertNotIn(9, {blink_ts_demux.type_nal(nal) for nal in nals})
        unites = self._alimenter([(None, nal) for nal in nals])
        self.assertEqual(len(unites), images)

        # Chaque unité doit être une image complète et décodable à elle seule
        # (après les précédentes), comme l'exige le paquétiseur RTP d'aiortc.
        decodeur = blink_webrtc.av.CodecContext.create("h264", "r")
        decodees = 0
        for _pts, unite in unites:
            decodees += len(decodeur.decode(blink_webrtc.av.Packet(unite)))
        decodees += len(decodeur.decode(None))
        self.assertEqual(decodees, images)


if __name__ == "__main__":
    unittest.main()
