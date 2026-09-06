"""Le diagnostic embarqué fonctionne sans console ni compte Blink."""
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

import smoketest


class TestsSmokeWebRTC(unittest.TestCase):
    def test_rapport_succes_serialisable(self):
        with tempfile.TemporaryDirectory() as temporaire:
            rapport = Path(temporaire) / "rapport avec espaces.json"
            probe = mock.AsyncMock(return_value={"frames_decoded": 3, "closed": True})
            with mock.patch("webrtc_probe.verifier_webrtc", probe), \
                    mock.patch.object(smoketest.md, "find_ffmpeg", return_value="ffmpeg"), \
                    redirect_stdout(StringIO()):
                self.assertEqual(smoketest.diagnostic_webrtc(rapport), 0)
            resultat = json.loads(rapport.read_text(encoding="utf-8"))
            self.assertTrue(resultat["ok"])
            self.assertEqual(resultat["frames_decoded"], 3)
            probe.assert_awaited_once_with("ffmpeg")

    def test_erreur_native_est_preservee_dans_rapport(self):
        with tempfile.TemporaryDirectory() as temporaire:
            rapport = Path(temporaire) / "rapport.json"
            probe = mock.AsyncMock(side_effect=ImportError("DLL native absente"))
            with mock.patch("webrtc_probe.verifier_webrtc", probe), \
                    mock.patch.object(smoketest.md, "find_ffmpeg", return_value="ffmpeg"), \
                    redirect_stdout(StringIO()):
                self.assertEqual(smoketest.diagnostic_webrtc(rapport), 1)
            resultat = json.loads(rapport.read_text(encoding="utf-8"))
            self.assertFalse(resultat["ok"])
            self.assertIn("DLL native absente", resultat["error"])

    def test_mode_webrtc_ne_lance_pas_le_smoketest_general(self):
        with mock.patch("sys.argv", ["smoketest", "--webrtc", "--report", "resultat.json"]), \
                mock.patch.object(smoketest, "diagnostic_webrtc", return_value=0) as diagnostic, \
                mock.patch.object(smoketest.md, "find_ffmpeg") as general:
            self.assertEqual(smoketest.main(), 0)
        diagnostic.assert_called_once_with(Path("resultat.json"))
        general.assert_not_called()

    def test_rapport_seul_est_refuse(self):
        with mock.patch("sys.argv", ["smoketest", "--report", "resultat.json"]), \
                redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                smoketest.main()


if __name__ == "__main__":
    unittest.main()
