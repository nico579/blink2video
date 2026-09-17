"""Webhook sortant (issue GitHub #11) : un POST best-effort par fichier prêt."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import runtime


class TestsNotifierNouveauMedia(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_notif_webhook_")
        self.dossier = Path(self.temporaire.name)
        self.patch = mock.patch.object(runtime, "app_dir", return_value=self.dossier)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temporaire.cleanup()

    def regler_url(self, url: str) -> None:
        runtime.ecrire_reglages(
            usb_minutes=10, cloud_minutes=1, port=8765, timestamp=False,
            timezone="Europe/Paris", merge_jour=True, merge_semaine=False,
            merge_mois=False, download_auto=True, live_protocol="webrtc",
            webhook_notif_url=url,
        )

    def test_url_absente_ne_declenche_aucune_requete(self):
        with mock.patch("runtime.urllib.request.urlopen") as urlopen:
            runtime.notifier_nouveau_media("Jardin", Path("clip.mp4"), "clip")
        urlopen.assert_not_called()

    def test_url_reglee_poste_le_bon_corps_json(self):
        self.regler_url("https://exemple.invalid/notif")
        with mock.patch("runtime.urllib.request.urlopen") as urlopen:
            runtime.notifier_nouveau_media(
                "Jardin", Path("/donnees/Jardin/clip.mp4"), "clip")
        urlopen.assert_called_once()
        requete, = urlopen.call_args.args
        self.assertEqual(requete.full_url, "https://exemple.invalid/notif")
        self.assertEqual(requete.get_method(), "POST")
        self.assertEqual(requete.get_header("Content-type"), "application/json")
        self.assertEqual(
            json.loads(requete.data.decode("utf-8")),
            {"camera": "Jardin", "chemin": str(Path("/donnees/Jardin/clip.mp4")),
             "type": "clip"})
        self.assertEqual(urlopen.call_args.kwargs.get("timeout"), 10)

    def test_type_media_photo_transmis_tel_quel(self):
        self.regler_url("https://exemple.invalid/notif")
        with mock.patch("runtime.urllib.request.urlopen") as urlopen:
            runtime.notifier_nouveau_media("Salon", Path("photo.jpg"), "snapshot")
        requete, = urlopen.call_args.args
        self.assertEqual(json.loads(requete.data.decode("utf-8"))["type"], "snapshot")

    def test_echec_reseau_absorbe_sans_lever(self):
        self.regler_url("https://exemple.invalid/notif")
        with mock.patch("runtime.urllib.request.urlopen",
                        side_effect=OSError("connexion refusee")):
            runtime.notifier_nouveau_media("Jardin", Path("clip.mp4"), "clip")

    def test_url_invalide_absorbee_sans_lever(self):
        self.regler_url("https://exemple.invalid/notif")
        with mock.patch("runtime.urllib.request.urlopen",
                        side_effect=ValueError("URL malformee")):
            runtime.notifier_nouveau_media("Jardin", Path("clip.mp4"), "clip")


if __name__ == "__main__":
    unittest.main()
