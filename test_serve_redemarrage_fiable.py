"""Non-régression du relais HTTP vers stop/restart et des réglages atomiques."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import serve


class TestsRelaisRedemarrage(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = object.__new__(serve.Handler)
        self.reponses = []
        self.handler.send_json = lambda payload, status=200: self.reponses.append(
            (status, payload))

    def test_confirme_seulement_apres_creation_du_relais(self):
        ordre = []

        def demarrer(*args, **kwargs):
            ordre.append("relais")
            return mock.Mock()

        self.handler.send_json = lambda payload, status=200: ordre.append("reponse")
        with mock.patch.object(serve.runtime, "self_command",
                               side_effect=lambda *args: list(args)), \
             mock.patch.object(serve.runtime, "demarrer", side_effect=demarrer) as appel:
            self.handler.repondre_puis_redemarrer(["restart", "--sans-relance"])
        self.assertEqual(ordre, ["relais", "reponse"])
        commande = appel.call_args[0][0]
        self.assertEqual(commande[:2], ["restart", "--sans-relance"])
        self.assertIn("--delai", commande)

    def test_echec_de_creation_est_renvoye_au_navigateur(self):
        with mock.patch.object(serve.runtime, "self_command", return_value=["restart"]), \
             mock.patch.object(serve.runtime, "demarrer",
                               side_effect=OSError("exécutable introuvable")):
            self.handler.repondre_puis_redemarrer(["restart"])
        self.assertEqual(self.reponses[0][0], 500)
        self.assertIn("error", self.reponses[0][1])


class TestsReglagesTransactionnels(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink-reglages-http-")
        self.stockage = Path(self.temporaire.name) / "stockage"

    def tearDown(self) -> None:
        self.temporaire.cleanup()

    def handler(self, payload: dict):
        contenu = json.dumps(payload).encode("utf-8")
        handler = object.__new__(serve.Handler)
        handler.path = "/api/reglages"
        handler.headers = {"Content-Length": str(len(contenu))}
        handler.rfile = io.BytesIO(contenu)
        handler.hote_autorise = lambda: True
        handler.jeton_valide = lambda: True
        return handler

    def payload(self) -> dict:
        return {
            "usb_minutes": 10, "cloud_minutes": 1, "port": 8765,
            "storage_dir": str(self.stockage), "timestamp": False,
            "timezone": "Europe/Paris", "merge_jour": True,
            "merge_semaine": False, "merge_mois": False,
            "download_auto": True,
        }

    def test_bascule_puis_ecrit_les_reglages_dans_la_nouvelle_racine(self):
        ordre = []
        handler = self.handler(self.payload())
        handler.send_json = lambda *args, **kwargs: ordre.append("reponse")
        handler.repondre_puis_redemarrer = lambda *args: ordre.append("restart")
        with mock.patch.object(serve.runtime, "ecrire_dossier_stockage",
                               side_effect=lambda path: ordre.append("stockage")), \
             mock.patch.object(serve.runtime, "ecrire_reglages",
                               side_effect=lambda *args: ordre.append("reglages")):
            handler.do_POST()
        self.assertEqual(ordre, ["stockage", "reglages", "restart"])

    def test_echec_de_bascule_repond_en_erreur_sans_redemarrer(self):
        handler = self.handler(self.payload())
        reponses = []
        handler.send_json = lambda payload, status=200: reponses.append((status, payload))
        handler.repondre_puis_redemarrer = mock.Mock()
        with mock.patch.object(serve.runtime, "ecrire_dossier_stockage",
                               side_effect=OSError("pointeur verrouillé")), \
             mock.patch.object(serve.runtime, "ecrire_reglages") as ecrire_reglages:
            handler.do_POST()
        self.assertEqual(reponses[0][0], 500)
        self.assertIn("error", reponses[0][1])
        ecrire_reglages.assert_not_called()
        handler.repondre_puis_redemarrer.assert_not_called()

    def test_modification_concurrente_repond_conflit(self):
        handler = self.handler(self.payload())
        reponses = []
        handler.send_json = lambda payload, status=200: reponses.append((status, payload))
        handler.repondre_puis_redemarrer = mock.Mock()
        with mock.patch.object(serve.runtime, "verrou_configuration",
                               side_effect=serve.runtime.BusyError("occupé")), \
             mock.patch.object(serve.runtime, "ecrire_dossier_stockage") as stockage:
            handler.do_POST()
        self.assertEqual(reponses[0][0], 409)
        stockage.assert_not_called()
        handler.repondre_puis_redemarrer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
