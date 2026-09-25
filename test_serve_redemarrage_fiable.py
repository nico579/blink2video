"""Non-régression du relais HTTP vers stop/restart et des réglages atomiques."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Lancé seul, ce fichier écrivait sinon dans le vrai dossier d'état (jeton
# de webhook créé par GET /api/reglages).
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-redemarrage-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import serve  # noqa: E402 - environnement isolé avant import


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

    def test_api_redemarrer_relaie_un_restart_simple_sans_toucher_aux_reglages(self):
        # Bouton dédié (pas Appliquer) : un restart tout court, sans passer
        # par _preparer_reglages_web ni ecrire_dossier_stockage.
        handler = object.__new__(serve.Handler)
        handler.path = "/api/redemarrer"
        handler.headers = {"Content-Length": "2"}
        handler.rfile = io.BytesIO(b"{}")
        handler.hote_autorise = lambda: True
        handler.jeton_valide = lambda: True
        handler.repondre_puis_redemarrer = mock.Mock()
        with mock.patch.object(serve.runtime, "ecrire_dossier_stockage") as stockage:
            handler.do_POST()
        handler.repondre_puis_redemarrer.assert_called_once_with(["restart"])
        stockage.assert_not_called()


class TestsModeConfigurationInitiale(unittest.TestCase):
    def test_actualisation_manuelle_ne_peut_pas_telecharger_avant_validation(self):
        handler = object.__new__(serve.Handler)
        handler.path = "/api/refresh"
        handler.initial_setup = True
        handler.hote_autorise = lambda: True
        handler.jeton_valide = lambda: True
        handler.stream_refresh = mock.Mock()
        reponses = []
        handler.send_json = lambda payload, status=200: reponses.append((status, payload))
        handler.do_GET()
        self.assertEqual(reponses[0][0], 409)
        handler.stream_refresh.assert_not_called()

    def test_api_reglages_annonce_le_mode_initial_a_toute_ouverture(self):
        handler = object.__new__(serve.Handler)
        handler.path = "/api/reglages"
        handler.initial_setup = True
        handler.hote_autorise = lambda: True
        handler.jeton_valide = lambda: True
        reponses = []
        handler.send_json = lambda payload, status=200: reponses.append(payload)
        with mock.patch.object(serve.runtime, "lire_reglages", return_value={}), \
             mock.patch.object(serve.runtime, "lire_dossier_stockage",
                               return_value="C:/donnees"):
            handler.do_GET()
        self.assertTrue(reponses[0]["initial_setup"])
        self.assertEqual(reponses[0]["storage_dir"], "C:/donnees")


class TestsReglagesTransactionnels(unittest.TestCase):
    """Le dossier choisi dans la page est celui des sorties, enregistré avec
    les autres réglages dans le dossier d'état, qui lui ne bouge pas."""

    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink-reglages-http-")
        self.addCleanup(self.temporaire.cleanup)
        racine = Path(self.temporaire.name).resolve()
        self.etat = racine / "etat"
        self.stockage = racine / "stockage"
        self.defaut = racine / "Documents" / serve.runtime.ENTREE
        (racine / "programme").mkdir()
        for correctif in (
                mock.patch.object(serve.runtime, "_dossier_ancre",
                                  return_value=racine / "programme"),
                mock.patch.object(serve.runtime, "_dossier_etat_standard",
                                  return_value=self.etat),
                mock.patch.object(serve.runtime, "_ETATS_CREES", set()),
                mock.patch("platformdirs.user_documents_dir",
                           return_value=str(racine / "Documents")),
                mock.patch.dict(os.environ, {}, clear=False)):
            correctif.start()
            self.addCleanup(correctif.stop)
        os.environ.pop("BLINK_HOME", None)
        os.environ.pop("BLINK_CONTROL_HOME", None)

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

    def reglages(self) -> bytes:
        return (self.etat / serve.runtime.REGLAGES).read_bytes()

    def test_ecrit_les_reglages_et_le_dossier_puis_redemarre(self):
        ordre = []
        handler = self.handler(self.payload())
        handler.send_json = lambda *args, **kwargs: ordre.append("reponse")
        handler.repondre_puis_redemarrer = lambda *args: ordre.append("restart")
        ecrire = serve.runtime.ecrire_reglages

        def ecrire_avec_le_dossier(**kwargs):
            self.assertNotIn("dossier", kwargs)
            self.assertEqual(kwargs["dossier_sorties"], str(self.stockage))
            ordre.append("reglages")
            ecrire(**kwargs)

        with mock.patch.object(serve.runtime, "ecrire_reglages",
                               side_effect=ecrire_avec_le_dossier):
            handler.do_POST()
        self.assertEqual(ordre, ["reglages", "restart"])
        self.assertEqual(serve.runtime.app_dir(), self.etat)
        self.assertEqual(serve.runtime.dossier_sorties(), self.stockage)
        self.assertTrue(self.stockage.is_dir())
        self.assertEqual(serve.runtime.lire_reglages()["port"], 8765)

    def test_echec_de_bascule_repond_en_erreur_sans_redemarrer(self):
        handler = self.handler(self.payload())
        reponses = []
        handler.send_json = lambda payload, status=200: reponses.append((status, payload))
        handler.repondre_puis_redemarrer = mock.Mock()
        with mock.patch.object(serve.runtime, "ecrire_dossier_stockage",
                               side_effect=OSError("réglages verrouillés")), \
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

    def test_configuration_initiale_marque_puis_repond_sans_restart(self):
        """Le parent start, encore propriétaire du verrou de démarrage,
        effectuera lui-même la transition vers les workers complets."""
        ordre = []
        handler = self.handler(self.payload())
        handler.initial_setup = True
        handler.send_json = lambda payload, status=200: ordre.append(
            ("reponse", status, payload))
        handler.repondre_puis_redemarrer = mock.Mock()
        marquer = serve.runtime.marquer_configuration_initiale

        def marquer_apres_les_reglages():
            self.assertEqual(serve.runtime.dossier_sorties(), self.stockage)
            ordre.append("marqueur")
            marquer()

        with mock.patch.object(serve.runtime, "marquer_configuration_initiale",
                               side_effect=marquer_apres_les_reglages):
            handler.do_POST()
        self.assertEqual(ordre[0], "marqueur")
        self.assertEqual(ordre[1][0], "reponse")
        self.assertTrue(ordre[1][2]["initial_setup"])
        self.assertTrue(serve.runtime.configuration_initiale_effectuee())
        handler.repondre_puis_redemarrer.assert_not_called()

    def test_echec_du_marqueur_initial_ne_lance_ni_restart_ni_workers(self):
        handler = self.handler(self.payload())
        handler.initial_setup = True
        reponses = []
        handler.send_json = lambda payload, status=200: reponses.append((status, payload))
        handler.repondre_puis_redemarrer = mock.Mock()
        with mock.patch.object(serve.runtime, "marquer_configuration_initiale",
                               side_effect=OSError("état non inscriptible")):
            handler.do_POST()
        self.assertEqual(reponses[0][0], 500)
        self.assertIn("error", reponses[0][1])
        handler.repondre_puis_redemarrer.assert_not_called()
        # Aucun réglage n'existait : il n'en reste aucun.
        self.assertFalse((self.etat / serve.runtime.REGLAGES).exists())
        self.assertEqual(serve.runtime.dossier_sorties(), self.defaut)
        self.assertFalse(serve.runtime.configuration_initiale_effectuee())

    def test_echec_des_reglages_ne_change_rien(self):
        serve.runtime.app_dir()
        anciens = b'{"port": 9999}'
        (self.etat / serve.runtime.REGLAGES).write_bytes(anciens)
        handler = self.handler(self.payload())
        reponses = []
        handler.send_json = lambda payload, status=200: reponses.append((status, payload))
        handler.repondre_puis_redemarrer = mock.Mock()
        with mock.patch.object(serve.runtime, "ecrire_reglages",
                               side_effect=PermissionError("réglages verrouillés")):
            handler.do_POST()
        self.assertEqual(reponses[0][0], 500)
        self.assertEqual(self.reglages(), anciens)
        handler.repondre_puis_redemarrer.assert_not_called()

    def test_echec_du_marqueur_restaure_les_reglages_existants(self):
        serve.runtime.ecrire_dossier_stockage(str(self.stockage))
        anciens = self.reglages()
        valeurs = self.payload()
        valeurs["storage_dir"] = ""
        handler = self.handler(valeurs)
        handler.initial_setup = True
        reponses = []
        handler.send_json = lambda payload, status=200: reponses.append((status, payload))
        handler.repondre_puis_redemarrer = mock.Mock()
        with mock.patch.object(serve.runtime, "marquer_configuration_initiale",
                               side_effect=PermissionError("marqueur verrouillé")):
            handler.do_POST()
        self.assertEqual(reponses[0][0], 500)
        self.assertEqual(self.reglages(), anciens)
        self.assertEqual(serve.runtime.dossier_sorties(), self.stockage)
        self.assertFalse(serve.runtime.configuration_initiale_effectuee())
        handler.repondre_puis_redemarrer.assert_not_called()

    def test_blink_home_ecrit_dans_la_racine_forcee(self):
        force = self.etat.parent / "force"
        force.mkdir()
        handler = self.handler(self.payload())
        handler.send_json = mock.Mock()
        handler.repondre_puis_redemarrer = mock.Mock()
        with mock.patch.dict(os.environ, {"BLINK_HOME": str(force)}):
            handler.do_POST()
            self.assertEqual(serve.runtime.app_dir(), force)
            self.assertTrue((force / serve.runtime.REGLAGES).is_file())
            self.assertEqual(serve.runtime.dossier_sorties(), self.stockage)
        self.assertFalse((self.etat / serve.runtime.REGLAGES).exists())
        handler.repondre_puis_redemarrer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
