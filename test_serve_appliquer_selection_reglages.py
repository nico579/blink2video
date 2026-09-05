"""Non-régression : écarter/réintégrer un clip (/api/appliquer-selection)
doit respecter les réglages de la page (Quotidienne, Incrustation) au lieu
de toujours reconstruire une quotidienne horodatée en sous-main, quel que
soit ce que l'utilisateur a coché (bug constaté en réel, 2026-09-05 : un
ffmpeg tournait encore alors que Quotidienne et Incrustation étaient
décochées)."""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8, édition Windows 7
    from backports.zoneinfo import ZoneInfo

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-selection-reglages-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import runtime  # noqa: E402 - environnement de test posé avant import
import serve  # noqa: E402 - bootstrap neutralisé avant import


class TestAppliquerSelectionRespecteLesReglages(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_selection_reglages_test_")
        self.racine = Path(self.temporaire.name)
        self.paths = {
            "input": self.racine / "clips",
            "normalized": self.racine / "normalized",
            "excluded": self.racine / "excluded",
            "thumbs": self.racine / "thumbs",
        }
        for chemin in self.paths.values():
            chemin.mkdir(parents=True, exist_ok=True)

        self.patch_app_dir = mock.patch.object(runtime, "app_dir", return_value=self.racine)
        self.patch_app_dir.start()
        self.patch_lancer = mock.patch.object(
            runtime, "lancer", return_value=mock.Mock(stdout=""),
        )
        self.appels_lancer = self.patch_lancer.start()

        self.identity = "jardin/test.mp4"
        (self.paths["input"] / "jardin").mkdir(parents=True, exist_ok=True)
        (self.paths["input"] / self.identity).write_bytes(b"\x00" * 16)
        registre = {
            "version": 2,
            "clips": {
                "cle-1": {
                    "path": self.identity,
                    "camera": "jardin",
                    "created_at": "2026-08-18T12:00:00+00:00",
                    "hub": "Maison",
                    "source": "usb",
                }
            },
        }
        (self.paths["input"] / ".blink_download_state.json").write_text(
            json.dumps(registre), encoding="utf-8")

    def tearDown(self) -> None:
        self.patch_lancer.stop()
        self.patch_app_dir.stop()
        self.temporaire.cleanup()

    def construire_handler(self):
        handler = serve.Handler.__new__(serve.Handler)
        handler.paths = self.paths
        handler.timezone = ZoneInfo("UTC")
        return handler

    def ecarter(self, handler):
        corps = json.dumps({"exclure": [self.identity]}).encode("utf-8")
        handler.path = "/api/appliquer-selection"
        handler.headers = {"Content-Length": str(len(corps)), "Host": "127.0.0.1",
                          "X-Blink-Token": serve.TOKEN}
        handler.rfile = io.BytesIO(corps)
        handler.send_json = lambda payload, code=200: None
        handler.do_POST()

    def attendre_exclusion_appliquee(self) -> None:
        # travailler_registre() applique l'exclusion, PUIS décide de
        # reconstruire ou non : sans attendre la première étape, un test sur
        # runtime.lancer pourrait s'exécuter avant même que le fil de fond
        # n'ait pris sa décision.
        for _ in range(30):
            etat = serve.blink_registre.load_download_state(self.paths["input"])
            if etat["clips"].get("cle-1", {}).get("excluded"):
                return
            time.sleep(0.1)
        self.fail("l'exclusion n'a jamais été appliquée en arrière-plan")

    def test_quotidienne_decochee_ne_reconstruit_rien(self):
        with mock.patch.object(
            runtime, "lire_reglages",
            return_value={**runtime.REGLAGES_DEFAUT, "merge_jour": False},
        ):
            handler = self.construire_handler()
            self.ecarter(handler)
            self.attendre_exclusion_appliquee()
            # Rien de plus à attendre ici : REASSEMBLAGE n'est jamais pris,
            # laisser une marge courte suffit à détecter un appel tardif.
            time.sleep(0.2)
        # assert_not_called() est trop strict : runtime.lancer sert aussi,
        # sous Unix, a identite_processus() pour le verrou du registre
        # (ps -o lstart=, hors sujet ici mais bel et bien un vrai appel
        # declenche par l'exclusion elle-meme). Seule une commande "merge"
        # doit etre absente.
        commandes_merge = [appel.args[0] for appel in self.appels_lancer.call_args_list
                           if appel.args and "merge" in appel.args[0]]
        self.assertEqual(commandes_merge, [],
                         "reconstruction lancee alors que Quotidienne est decochee")

    def test_incrustation_decochee_passe_no_timestamp(self):
        with mock.patch.object(
            runtime, "lire_reglages",
            return_value={**runtime.REGLAGES_DEFAUT, "merge_jour": True, "timestamp": False},
        ):
            handler = self.construire_handler()
            self.ecarter(handler)
            self.attendre_exclusion_appliquee()
            for _ in range(30):
                if self.appels_lancer.called:
                    break
                time.sleep(0.1)
            else:
                self.fail("merge --camera/--date n'a jamais été lancé")
        commande = self.appels_lancer.call_args.args[0]
        self.assertIn("--no-timestamp", commande)

    def test_incrustation_cochee_omet_no_timestamp(self):
        with mock.patch.object(
            runtime, "lire_reglages",
            return_value={**runtime.REGLAGES_DEFAUT, "merge_jour": True, "timestamp": True},
        ):
            handler = self.construire_handler()
            self.ecarter(handler)
            self.attendre_exclusion_appliquee()
            for _ in range(30):
                if self.appels_lancer.called:
                    break
                time.sleep(0.1)
            else:
                self.fail("merge --camera/--date n'a jamais été lancé")
        commande = self.appels_lancer.call_args.args[0]
        self.assertNotIn("--no-timestamp", commande)


if __name__ == "__main__":
    unittest.main()
