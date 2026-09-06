"""Choix du décodeur distant, sans caméra ni modification du flux H.264."""
import unittest

import blink_webrtc


@unittest.skipUnless(blink_webrtc.DISPONIBLE, "aiortc optionnel")
class TestsProfilsReception(unittest.TestCase):
    def test_payloads_dynamiques_chromium_conserves_sans_empieter_sur_rtcp(self):
        from aiortc import rtp
        from aiortc.rtcpeerconnection import find_common_codecs
        from aiortc.rtcrtpparameters import RTCRtpCodecParameters
        parametres = {"packetization-mode": "1", "profile-level-id": "f4001f"}
        local = RTCRtpCodecParameters(mimeType="video/H264", clockRate=90000,
                                     payloadType=112, parameters=parametres)
        distant = RTCRtpCodecParameters(mimeType="video/H264", clockRate=90000,
                                       payloadType=41, parameters=parametres)
        self.assertEqual(find_common_codecs([local], [distant])[0].payloadType, 41)
        self.assertEqual(local.payloadType, 112)
        self.assertTrue(set(range(35, 64)).issubset(rtp.DYNAMIC_PAYLOAD_TYPES))
        self.assertFalse(set(range(64, 96)).intersection(rtp.DYNAMIC_PAYLOAD_TYPES))

    def offre(self, profils, *, direction="recvonly", mode="1", port=9, kind="video"):
        lignes = ["v=0", "o=- 0 0 IN IP4 127.0.0.1", "s=-", "t=0 0",
                  f"m={kind} {port} UDP/TLS/RTP/SAVPF " + " ".join(str(100+i) for i in range(len(profils))),
                  f"a={direction}"]
        for i, profil in enumerate(profils):
            lignes.extend([f"a=rtpmap:{100+i} H264/90000",
                           f"a=fmtp:{100+i} packetization-mode={mode};profile-level-id={profil};level-asymmetry-allowed=1"])
        return "\r\n".join(lignes) + "\r\n"

    def test_prefere_high_exact_si_disponible(self):
        self.assertEqual(blink_webrtc._profil_reception_h264(
            self.offre(["f4001f", "64001f"]), "640028"), "640028")

    def test_high444_accepte_high_mais_pas_d_annonce_baseline(self):
        self.assertEqual(blink_webrtc._profil_reception_h264(
            self.offre(["42e01f", "f4001f"]), "640028"), "f40028")
        self.assertEqual(blink_webrtc._profil_reception_h264(
            self.offre(["42e01f", "4d001f"]), "640028"), "640028")

    def test_ignore_codec_invalide_ou_section_non_receptrice(self):
        for options in ({"direction": "sendonly"}, {"direction": "inactive"},
                        {"mode": "0"}, {"port": 0}, {"kind": "audio"}):
            with self.subTest(options=options):
                self.assertEqual(blink_webrtc._profil_reception_h264(
                    self.offre(["f4001f"], **options), "640028"), "640028")
        self.assertEqual(blink_webrtc._profil_reception_h264(
            self.offre(["invalide"]), "640028"), "640028")
        self.assertEqual(blink_webrtc._profil_reception_h264(
            self.offre(["f4101f"]), "640028"), "640028")

    def test_ne_modifie_pas_les_autres_profils(self):
        self.assertEqual(blink_webrtc._profil_reception_h264(
            self.offre(["f4001f"]), "42e01f"), "42e01f")


if __name__ == "__main__":
    unittest.main()
