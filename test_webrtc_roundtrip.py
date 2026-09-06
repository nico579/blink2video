"""Vrai H.264/ICE/DTLS/SRTP/décodage local, également exécutable sous Win7."""

import importlib.util
import json
import os
import shutil
import unittest
from unittest import mock

import runtime
from webrtc_probe import verifier_webrtc


def trouver_ffmpeg():
    # Permet de tester un venv minimal avec le binaire déjà livré ailleurs.
    explicite = os.environ.get("FFMPEG_BINARY")
    if explicite:
        return explicite  # Un chemin déclaré mais cassé doit échouer, pas skip.
    systeme = shutil.which("ffmpeg")
    if systeme:
        return systeme
    for motif in ("ffmpeg*.exe", "ffmpeg-*", "ffmpeg"):
        for fichier in sorted(runtime.resource_dir().glob(motif)):
            if fichier.is_file():
                return str(fichier)
    if importlib.util.find_spec("imageio_ffmpeg") is not None:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    return None


@unittest.skipUnless(importlib.util.find_spec("aiortc"), "aiortc non installé")
class TestsWebRTCRoundtrip(unittest.IsolatedAsyncioTestCase):
    async def test_high_recu_par_decodeur_high444_sans_reencodage(self):
        ffmpeg = trouver_ffmpeg()
        if ffmpeg is None:
            self.skipTest("ffmpeg non installé")
        with mock.patch("aiortc.codecs.h264.H264Encoder.encode",
                        side_effect=AssertionError("Réencodage interdit")):
            resultat = await verifier_webrtc(ffmpeg, profil_recepteur="high444", payload_type=41)
        self.assertTrue(resultat["profile_level_id"].startswith("64"))
        self.assertTrue(resultat["negotiated_profile_level_id"].startswith("f400"))
        self.assertEqual(resultat["frames_decoded"], 3)
        self.assertEqual(resultat["payload_type"], 41)
        self.assertTrue(resultat["closed"])

    async def test_h264_high_negocie_transfere_decode_et_ferme(self):
        ffmpeg = trouver_ffmpeg()
        if ffmpeg is None:
            self.skipTest("ffmpeg non installé")
        # Les erreurs d'import natif (av, SRTP, OpenSSL...) ne sont pas
        # escamotées par DISPONIBLE=False : un build annoncé doit fonctionner.
        import aioice.ice
        import blink_webrtc
        candidats_avant = aioice.ice.get_host_addresses
        codecs_avant = list(blink_webrtc.CODECS["video"])
        profils_avant = set(blink_webrtc._PROFILS_ENREGISTRES)
        resultat = await verifier_webrtc(ffmpeg)
        json.dumps(resultat)
        self.assertEqual(resultat["codec"], "H264")
        self.assertTrue(resultat["profile_level_id"].startswith("64"))
        self.assertGreaterEqual(resultat["frames_decoded"], 3)
        self.assertGreater(resultat["packets_received"], 0)
        self.assertEqual((resultat["width"], resultat["height"]), (160, 96))
        self.assertTrue(resultat["closed"])
        self.assertTrue(resultat["loopback_only"])
        self.assertEqual(resultat["on_close_calls"], 1)
        self.assertLess(resultat["elapsed_seconds"], 30)
        self.assertIs(aioice.ice.get_host_addresses, candidats_avant)
        self.assertEqual(blink_webrtc.CODECS["video"], codecs_avant)
        self.assertEqual(blink_webrtc._PROFILS_ENREGISTRES, profils_avant)


if __name__ == "__main__":
    unittest.main()
