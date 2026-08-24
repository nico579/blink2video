"""Non-régression du blocage de /api/appliquer-selection par le verrou
inter-processus « registre » (AUDIT-2026-08-13.md, section 28.33, puis
28.75 : /api/toggle a été remplacé par /api/appliquer-selection pour traiter
un lot de clips en un seul appel) : la réponse HTTP ne doit toujours pas
attendre ce verrou, seulement le travail qu'il protège en profite."""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8, édition Windows 7
    from backports.zoneinfo import ZoneInfo

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-toggle-lock-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import runtime
import serve  # noqa: E402 - bootstrap neutralisé avant import


class TestAppliquerSelectionNAttendPasLeVerrouRegistre(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_toggle_lock_test_")
        self.racine = Path(self.temporaire.name)
        self.paths = {
            "input": self.racine / "clips",
            "normalized": self.racine / "normalized",
            "excluded": self.racine / "excluded",
            "thumbs": self.racine / "thumbs",
        }
        for chemin in self.paths.values():
            chemin.mkdir(parents=True, exist_ok=True)

        # runtime.verrou() écrit son fichier sous app_dir() : redirigé vers
        # le dossier du test pour ne jamais toucher une vraie instance.
        self.patch_app_dir = mock.patch.object(runtime, "app_dir", return_value=self.racine)
        self.patch_app_dir.start()
        # Hors sujet ici (28.32/28.9) : ce test porte sur le verrou registre,
        # pas sur la reconstruction. Sans ce mock, le thread de fond lance un
        # vrai sous-processus « merge » qui peut encore tenir un descripteur
        # sur le dossier temporaire au moment du nettoyage (PermissionError
        # Windows, tempfile._rmtree en boucle).
        self.patch_lancer = mock.patch.object(runtime, "lancer")
        self.patch_lancer.start()

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
        # Un vrai Handler, sans passer par __init__ (qui attend une vraie
        # socket) : do_POST ne lit que self.path/.headers/.rfile, faciles à
        # simuler sans ouvrir de connexion réseau.
        handler = serve.Handler.__new__(serve.Handler)
        handler.paths = self.paths
        handler.timezone = ZoneInfo("UTC")
        return handler

    def appeler(self, handler, corps_dict: dict):
        corps = json.dumps(corps_dict).encode("utf-8")
        handler.path = "/api/appliquer-selection"
        # Host + jeton requis depuis 28.60 (protection CSRF/Origin) : sans
        # eux, ce handler construit à la main est rejeté en 403.
        handler.headers = {"Content-Length": str(len(corps)), "Host": "127.0.0.1",
                          "X-Blink-Token": serve.TOKEN}
        handler.rfile = io.BytesIO(corps)
        reponses = []
        handler.send_json = lambda payload, code=200: reponses.append((code, payload))
        handler.do_POST()
        return reponses

    def test_la_reponse_ne_bloque_plus_sur_le_verrou_tenu_ailleurs(self):
        # Simule une écriture de téléchargement en cours : le même verrou
        # que set_excluded() prend en interne, tenu ici depuis un autre fil,
        # comme le ferait un vrai processus séparé.
        pret = threading.Event()
        relacher = threading.Event()

        def tenir_le_verrou():
            with runtime.verrou("registre", "test-telechargement", stale_after=60, attente=0):
                pret.set()
                relacher.wait(timeout=5)

        fil_verrou = threading.Thread(target=tenir_le_verrou, daemon=True)
        fil_verrou.start()
        self.assertTrue(pret.wait(timeout=2), "le verrou de test n'a pas été pris à temps")

        try:
            handler = self.construire_handler()
            debut = time.monotonic()
            reponses = self.appeler(handler, {"exclure": [self.identity]})
            duree = time.monotonic() - debut
        finally:
            relacher.set()
            fil_verrou.join(timeout=5)

        self.assertLess(duree, 1.0,
                         f"la réponse a attendu {duree:.2f}s le verrou tenu ailleurs")
        self.assertEqual(reponses, [(200, {"ok": True, "resultats": {}})])

        # Le travail réel (l'exclusion) doit tout de même aboutir une fois le
        # verrou libre, même si la réponse HTTP n'a pas attendu dessus.
        etat_fichier = self.paths["input"] / ".blink_download_state.json"
        for _ in range(30):
            etat = json.loads(etat_fichier.read_text(encoding="utf-8"))
            if etat["clips"]["cle-1"].get("excluded"):
                break
            time.sleep(0.1)
        else:
            self.fail("l'exclusion n'a jamais été appliquée en arrière-plan")
        self.assertTrue((self.paths["excluded"] / self.identity).is_file(),
                         "le brut aurait dû être déplacé vers Blink_Excluded")

    def test_identifiant_invalide_est_filtre_sans_verrou(self):
        # Un identifiant hors-forme (tentative de traversée de chemin) ne
        # doit jamais atteindre set_excluded : /api/appliquer-selection le
        # retire silencieusement de la liste plutôt que de rejeter tout le
        # lot, un client légitime (les cases de la page) ne pouvant de toute
        # façon pas en produire un.
        handler = self.construire_handler()
        reponses = self.appeler(handler, {"exclure": ["../../etc/passwd"]})
        self.assertEqual(reponses, [(200, {"ok": True, "resultats": {}})])
        etat_fichier = self.paths["input"] / ".blink_download_state.json"
        etat = json.loads(etat_fichier.read_text(encoding="utf-8"))
        self.assertNotIn("excluded", etat["clips"]["cle-1"],
                          "un identifiant invalide n'aurait jamais dû être appliqué")


if __name__ == "__main__":
    unittest.main()
