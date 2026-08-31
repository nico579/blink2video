"""Non-régression de /api/sourdine (AUDIT-2026-08-13.md, section 28.38) :
mise en sourdine par caméra depuis le panneau de réglages, sans redémarrage.

Couvre aussi le bug vécu en conditions réelles le 2026-08-18 : une caméra
durablement hors de portée ("Portail") n'ayant jamais produit de clip était
absente de la liste proposée par GET /api/sourdine, qui ne regardait que le
registre des clips. Corrigé en complétant avec les caméras connues du dernier
état de watch.py (fichier local, pas d'appel réseau)."""

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
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-sourdine-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import runtime
import watch
import serve  # noqa: E402 - bootstrap neutralisé avant import


class TestSourdine(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_sourdine_test_")
        self.racine = Path(self.temporaire.name)
        self.paths = {
            "input": self.racine / "clips",
            "normalized": self.racine / "normalized",
            "excluded": self.racine / "excluded",
            "thumbs": self.racine / "thumbs",
        }
        for chemin in self.paths.values():
            chemin.mkdir(parents=True, exist_ok=True)

        registre = {
            "version": 2,
            "clips": {
                "cle-1": {
                    "path": "jardin/test.mp4",
                    "camera": "jardin",
                    "created_at": "2026-08-18T12:00:00+00:00",
                    "hub": "Maison",
                    "source": "usb",
                }
            },
        }
        (self.paths["input"] / ".blink_download_state.json").write_text(
            json.dumps(registre), encoding="utf-8")

        self.watch_state = self.racine / "watch_state.json"
        self.patch_watch_state = mock.patch.object(watch, "WATCH_STATE", self.watch_state)
        self.patch_watch_state.start()

    def tearDown(self) -> None:
        self.patch_watch_state.stop()
        self.temporaire.cleanup()

    def construire_handler(self):
        handler = serve.Handler.__new__(serve.Handler)
        handler.paths = self.paths
        handler.timezone = ZoneInfo("UTC")
        # Host requis depuis 28.60 (protection CSRF/Origin, hote_autorise())
        # sur do_GET comme do_POST : sans lui, ce handler construit à la
        # main (pas de vraie requête HTTP derrière) est rejeté en 403 avant
        # même d'atteindre la route testée.
        handler.headers = {
            "Host": "127.0.0.1",
            "X-Blink-Token": serve.TOKEN,
        }
        return handler

    def appeler_get(self, handler):
        handler.path = "/api/sourdine"
        reponses = []
        handler.send_json = lambda payload, code=200: reponses.append((code, payload))
        handler.do_GET()
        return reponses

    def appeler_post(self, handler, camera: str, ignored: bool):
        corps = json.dumps({"camera": camera, "ignored": ignored}).encode("utf-8")
        handler.path = "/api/sourdine"
        # Host déjà posé par construire_handler() ; do_POST exige en plus le
        # jeton anti-CSRF (28.60) - serve.TOKEN, généré une fois par
        # processus, est directement accessible ici (même processus).
        handler.headers = {**handler.headers, "Content-Length": str(len(corps)),
                          "X-Blink-Token": serve.TOKEN}
        handler.rfile = io.BytesIO(corps)
        reponses = []
        handler.send_json = lambda payload, code=200: reponses.append((code, payload))
        handler.do_POST()
        return reponses

    def test_get_liste_les_cameras_du_registre_de_clips(self):
        handler = self.construire_handler()
        reponses = self.appeler_get(handler)
        self.assertEqual(reponses, [(200, {"cameras": ["jardin"], "ignored": []})])

    def test_get_complete_avec_les_cameras_connues_de_watch_meme_sans_clip(self):
        # Cas vécu : "Portail" hors de portée depuis toujours n'a jamais
        # produit de clip, mais watch.py la connaît via l'API Blink.
        self.watch_state.write_text(json.dumps({
            "cameras": {"jardin": {"online": True}, "Portail": {"online": False}},
            "ignored": [],
        }), encoding="utf-8")
        handler = self.construire_handler()
        reponses = self.appeler_get(handler)
        self.assertEqual(reponses, [(200, {"cameras": ["Portail", "jardin"], "ignored": []})])

    def test_get_rend_la_liste_ignoree_depuis_watch_state(self):
        self.watch_state.write_text(json.dumps({
            "cameras": {"jardin": {"online": True}},
            "ignored": ["jardin"],
        }), encoding="utf-8")
        handler = self.construire_handler()
        reponses = self.appeler_get(handler)
        self.assertEqual(reponses, [(200, {"cameras": ["jardin"], "ignored": ["jardin"]})])

    @staticmethod
    def attendre_appel(lancer_simule, timeout=2.0):
        # do_POST répond depuis le fil principal sans attendre le fil de
        # fond qui appelle runtime.lancer() : le laisser rattraper la
        # réponse avant de vérifier l'appel, plutôt qu'un sleep fixe fragile.
        debut = time.monotonic()
        while not lancer_simule.called and time.monotonic() - debut < timeout:
            time.sleep(0.02)

    def test_post_repond_tout_de_suite_sans_lancer_le_sous_processus(self):
        # Le vrai `watch --ignore` interroge le compte Blink en réseau : un
        # test unitaire ne doit ni l'attendre ni le déclencher pour de vrai,
        # seulement vérifier que l'appel est correctement formé et détaché.
        with mock.patch.object(runtime, "lancer") as lancer_simule:
            handler = self.construire_handler()
            reponses = self.appeler_post(handler, "Portail", True)
            self.attendre_appel(lancer_simule)

        self.assertEqual(reponses, [(200, {"ok": True})])
        lancer_simule.assert_called_once()
        commande = lancer_simule.call_args.args[0]
        self.assertIn("--ignore", commande)
        self.assertIn("Portail", commande)

    def test_post_sans_camera_est_rejete(self):
        with mock.patch.object(runtime, "lancer") as lancer_simule:
            handler = self.construire_handler()
            reponses = self.appeler_post(handler, "", True)
            self.attendre_appel(lancer_simule, timeout=0.2)

        self.assertEqual(reponses, [(400, {"error": "Nom de caméra manquant."})])
        lancer_simule.assert_not_called()

    def test_post_unignore_transmet_loption_correcte(self):
        with mock.patch.object(runtime, "lancer") as lancer_simule:
            handler = self.construire_handler()
            self.appeler_post(handler, "Portail", False)
            self.attendre_appel(lancer_simule)

        commande = lancer_simule.call_args.args[0]
        self.assertIn("--unignore", commande)
        self.assertNotIn("--ignore", commande)


if __name__ == "__main__":
    unittest.main()
