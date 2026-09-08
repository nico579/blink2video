"""Caractérise la validation HTTP des réglages, sans stockage ni réseau réels."""

import io
import json
import os
import tempfile
import unittest
from unittest import mock


_IMPORT_HOME = tempfile.TemporaryDirectory(prefix="blink-reglages-validation-import-")
with mock.patch.dict(os.environ, {"BLINK_BOOTSTRAP": "none",
                                  "BLINK_HOME": _IMPORT_HOME.name}):
    import serve


ERREUR_NOMBRES = (
    "Les cadences doivent être des nombres de minutes d'au "
    "moins 1, et le port un nombre entre 1 et 65535.")
DEFAUTS = {
    "usb_minutes": 5, "cloud_minutes": 10, "port": 8765,
    "timestamp": False, "timezone": "UTC", "merge_jour": True,
    "merge_semaine": True, "merge_mois": True, "download_auto": True,
    "live_protocol": "webrtc",
}
CHAMPS_BOOLEENS = (
    "timestamp", "merge_jour", "merge_semaine", "merge_mois", "download_auto")


class TestsValidationReglagesHttp(unittest.TestCase):
    def setUp(self):
        self.observations = []

        def remplacer(objet, nom, **options):
            patch = mock.patch.object(objet, nom, **options)
            remplacement = patch.start()
            self.addCleanup(patch.stop)
            self.observations.append(remplacement)
            return remplacement

        self.stockage = remplacer(serve.runtime, "ecrire_dossier_stockage")
        self.verrou = remplacer(serve.runtime, "verrou_configuration")
        # La sonde fait partie du comportement testé, mais reste entièrement
        # simulée, y compris lorsque le payload désigne un chemin arbitraire.
        self.chemin = remplacer(serve, "Path")
        self.dossier = self.chemin.return_value.expanduser.return_value
        self.creer_sonde = remplacer(serve.tempfile, "NamedTemporaryFile")
        self.contexte_sonde = self.creer_sonde.return_value
        self.sonde = self.contexte_sonde.__enter__.return_value
        self.contexte_sonde.__exit__.return_value = False
        self.interdits = []
        for objet, nom in (
            (serve.runtime, "lancer"), (serve.runtime, "demarrer"),
            (serve.BLINK, "call"), (serve.threading, "Thread"),
        ):
            self.interdits.append(remplacer(
                objet, nom, side_effect=AssertionError("Effet réel interdit : " + nom)))

    def tearDown(self):
        for interdit in self.interdits:
            interdit.assert_not_called()

    def payload(self, **changements):
        return {"usb_minutes": 5, "cloud_minutes": 10, "port": 8765,
                "timezone": "UTC", **changements}

    def requete(self, payload):
        for observation in self.observations:
            observation.reset_mock()
        corps = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler = object.__new__(serve.Handler)
        handler.path = "/api/reglages"
        handler.paths = {}
        handler.initial_setup = False
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {
            "Host": "127.0.0.1:8765", "X-Blink-Token": serve.TOKEN,
            "Content-Length": str(len(corps)),
        }
        handler.rfile = io.BytesIO(corps)
        handler.send_json = mock.Mock()
        handler.send_error = mock.Mock()
        handler.repondre_puis_redemarrer = mock.Mock()
        handler.do_POST()
        handler.send_error.assert_not_called()
        return handler

    def assert_enregistre(self, handler, reglages, dossier=""):
        self.verrou.assert_called_once_with()
        self.stockage.assert_called_once_with(
            dossier, reglages=reglages, configuration_initiale=False)
        handler.repondre_puis_redemarrer.assert_called_once_with(["restart"])
        handler.send_json.assert_not_called()

    def assert_refuse(self, handler, message):
        handler.send_json.assert_called_once_with({"error": message}, 400)
        self.verrou.assert_not_called()
        self.stockage.assert_not_called()
        handler.repondre_puis_redemarrer.assert_not_called()

    def test_valeurs_par_defaut_et_absence_de_sonde_sans_dossier(self):
        handler = self.requete(self.payload())
        self.assert_enregistre(handler, DEFAUTS)
        self.chemin.assert_not_called()

    def test_normalisation_des_nombres_booleens_et_textes(self):
        handler = self.requete(self.payload(
            usb_minutes=" 3 ", cloud_minutes=12.9, port=" 65535 ",
            storage_dir="  archives locales  ", timezone="  UTC \t",
            timestamp="false", merge_jour=0, merge_semaine=None,
            merge_mois="", download_auto=[False], live_protocol="mse"))
        self.assert_enregistre(handler, {
            "usb_minutes": 3, "cloud_minutes": 12, "port": 65535,
            "timestamp": True, "timezone": "UTC", "merge_jour": False,
            "merge_semaine": False, "merge_mois": False,
            "download_auto": True, "live_protocol": "mse",
        }, "archives locales")
        self.chemin.assert_called_once_with("archives locales")

    def test_booleens_conservent_la_conversion_bool_du_payload(self):
        for valeur, attendu in (
            (False, False), (None, False), (0, False), ("", False),
            ([], False), ({}, False), (True, True), (1, True),
            ("false", True), ([0], True), ({"actif": False}, True),
        ):
            with self.subTest(valeur=valeur):
                champs = dict.fromkeys(CHAMPS_BOOLEENS, valeur)
                handler = self.requete(self.payload(**champs))
                self.assert_enregistre(
                    handler, {**DEFAUTS, **dict.fromkeys(CHAMPS_BOOLEENS, attendu)})

    def test_bornes_et_conversions_numeriques_acceptees(self):
        for valeurs, attendues in (
            ((1, 1, 1), (1, 1, 1)),
            ((1000000, 1000000, 65535), (1000000, 1000000, 65535)),
            ((True, True, True), (1, 1, 1)),
            ((" 2 ", "3", "4"), (2, 3, 4)),
            ((2.9, 3.9, 4.9), (2, 3, 4)),
        ):
            with self.subTest(valeurs=valeurs):
                champs = ("usb_minutes", "cloud_minutes", "port")
                handler = self.requete(self.payload(**dict(zip(champs, valeurs))))
                self.assert_enregistre(handler, {**DEFAUTS, **dict(zip(champs, attendues))})

    def test_nombres_absents_ou_invalides_refuses_avant_sonde(self):
        for champ in ("usb_minutes", "cloud_minutes", "port"):
            for valeur in (None, "", "invalide", [], {}, 0, -1, False, 0.9):
                with self.subTest(champ=champ, valeur=valeur):
                    handler = self.requete(self.payload(
                        **{champ: valeur, "storage_dir": "archives"}))
                    self.assert_refuse(handler, ERREUR_NOMBRES)
                    self.chemin.assert_not_called()
            with self.subTest(champ_absent=champ):
                payload = self.payload()
                del payload[champ]
                self.assert_refuse(self.requete(payload), ERREUR_NOMBRES)

    def test_port_superieur_a_65535_refuse(self):
        for valeur in (65536, "65536", 99999999):
            with self.subTest(valeur=valeur):
                self.assert_refuse(self.requete(self.payload(port=valeur)), ERREUR_NOMBRES)

    def test_debordement_numerique_ne_devient_pas_un_refus_de_formulaire(self):
        # La refactorisation ne doit pas élargir les exceptions absorbées par
        # le validateur : int(inf) lève historiquement une OverflowError.
        for champ in ("usb_minutes", "cloud_minutes", "port"):
            with self.subTest(champ=champ):
                with self.assertRaises(OverflowError):
                    self.requete(self.payload(**{champ: float("inf")}))
                self.chemin.assert_not_called()
                self.verrou.assert_not_called()
                self.stockage.assert_not_called()

    def test_erreurs_inattendues_de_sonde_restent_propagees(self):
        # Seules les erreurs d'accès OSError sont des refus 400 connus. Une
        # exception métier dédiée ne doit pas capturer tout ValueError.
        for erreur in (ValueError("chemin invalide"), RuntimeError("profil absent")):
            with self.subTest(erreur=type(erreur).__name__):
                self.chemin.return_value.expanduser.side_effect = erreur
                with self.assertRaises(type(erreur)) as capture:
                    self.requete(self.payload(storage_dir="archives"))
                self.assertIs(capture.exception, erreur)
                self.dossier.mkdir.assert_not_called()
                self.verrou.assert_not_called()
                self.stockage.assert_not_called()

    def test_dossier_vide_ou_blanc_ne_declenche_pas_de_sonde(self):
        for valeur in ("", " \t \n"):
            with self.subTest(valeur=valeur):
                handler = self.requete(self.payload(storage_dir=valeur))
                self.assert_enregistre(handler, DEFAUTS)
                self.chemin.assert_not_called()

    def test_dossier_converti_en_texte_avant_sonde(self):
        for valeur, attendu in ((None, "None"), (123, "123")):
            with self.subTest(valeur=valeur):
                handler = self.requete(self.payload(storage_dir=valeur))
                self.assert_enregistre(handler, DEFAUTS, attendu)
                self.chemin.assert_called_once_with(attendu)

    def test_sonde_cree_ecrit_puis_efface_avant_enregistrement(self):
        operations = mock.Mock()
        operations.attach_mock(self.dossier.mkdir, "mkdir")
        operations.attach_mock(self.creer_sonde, "creer_sonde")
        operations.attach_mock(self.sonde.write, "write")
        operations.attach_mock(self.sonde.flush, "flush")
        operations.attach_mock(self.contexte_sonde.__exit__, "fermer_et_effacer")
        operations.attach_mock(self.stockage, "enregistrer")
        handler = self.requete(self.payload(storage_dir="archives"))
        self.assert_enregistre(handler, DEFAUTS, "archives")
        self.chemin.return_value.expanduser.assert_called_once_with()
        self.assertEqual(operations.mock_calls, [
            mock.call.mkdir(parents=True, exist_ok=True),
            mock.call.creer_sonde(mode="w", encoding="utf-8",
                                  prefix=".blink_ecriture_test-", dir=self.dossier),
            mock.call.creer_sonde().__enter__(),
            mock.call.write("blink2video"), mock.call.flush(),
            mock.call.fermer_et_effacer(None, None, None),
            mock.call.enregistrer("archives", reglages=DEFAUTS, configuration_initiale=False),
        ])

    def test_erreur_creation_dossier_renvoie_400_sans_ecriture_sonde(self):
        self.dossier.mkdir.side_effect = OSError("création refusée")
        handler = self.requete(self.payload(storage_dir="archives"))
        self.assert_refuse(handler, "Dossier de stockage inaccessible : création refusée")
        self.creer_sonde.assert_not_called()

    def test_erreur_creation_sonde_renvoie_400_sans_enregistrement(self):
        self.creer_sonde.side_effect = OSError("création sonde refusée")
        handler = self.requete(self.payload(storage_dir="archives"))
        self.assert_refuse(handler, "Dossier de stockage inaccessible : création sonde refusée")
        self.sonde.write.assert_not_called()
        self.contexte_sonde.__exit__.assert_not_called()

    def test_erreur_ecriture_sonde_renvoie_400_et_nettoie(self):
        self.sonde.write.side_effect = OSError("écriture refusée")
        handler = self.requete(self.payload(storage_dir="archives"))
        self.assert_refuse(handler, "Dossier de stockage inaccessible : écriture refusée")
        self.dossier.mkdir.assert_called_once_with(parents=True, exist_ok=True)
        self.contexte_sonde.__exit__.assert_called_once()

    def test_erreur_vidage_sonde_renvoie_400_et_nettoie(self):
        self.sonde.flush.side_effect = OSError("disque plein")
        handler = self.requete(self.payload(storage_dir="archives"))
        self.assert_refuse(handler, "Dossier de stockage inaccessible : disque plein")
        self.contexte_sonde.__exit__.assert_called_once()

    def test_erreur_effacement_sonde_renvoie_400_sans_enregistrement(self):
        self.contexte_sonde.__exit__.side_effect = OSError("effacement refusé")
        handler = self.requete(self.payload(storage_dir="archives"))
        self.assert_refuse(handler, "Dossier de stockage inaccessible : effacement refusé")
        self.sonde.write.assert_called_once_with("blink2video")

    def test_fuseaux_absents_invalides_ou_vides_refuses(self):
        for valeur, texte in (
            ("", ""), ("  ", ""), (None, "None"),
            (" Zone/Inexistante ", "Zone/Inexistante"), ("../UTC", "../UTC"),
        ):
            with self.subTest(valeur=valeur):
                self.assert_refuse(self.requete(self.payload(timezone=valeur)),
                                   f"Fuseau horaire inconnu : « {texte} ».")
        payload = self.payload()
        del payload["timezone"]
        self.assert_refuse(self.requete(payload), "Fuseau horaire inconnu : «  ».")

    def test_protocoles_webrtc_et_mse_acceptes(self):
        for protocole in ("webrtc", "mse"):
            with self.subTest(protocole=protocole):
                handler = self.requete(self.payload(live_protocol=protocole))
                self.assert_enregistre(handler, {**DEFAUTS, "live_protocol": protocole})

    def test_protocoles_invalides_refuses_sans_normalisation(self):
        for valeur, texte in (
            (None, "None"), ("", ""), ("MSE", "MSE"), ("WebRTC", "WebRTC"),
            (" mse ", " mse "), ("baseline", "baseline"), (123, "123"),
        ):
            with self.subTest(valeur=valeur):
                self.assert_refuse(self.requete(self.payload(live_protocol=valeur)),
                                   f"Protocole de direct inconnu : « {texte} ».")

    def test_ordre_des_refus_nombres_dossier_fuseau_puis_protocole(self):
        payload = self.payload(usb_minutes=0, storage_dir="archives",
                               timezone="Zone/Inexistante", live_protocol="inconnu")
        self.dossier.mkdir.side_effect = OSError("sonde refusée")
        self.assert_refuse(self.requete(payload), ERREUR_NOMBRES)
        self.chemin.assert_not_called()

        payload["usb_minutes"] = 1
        self.assert_refuse(self.requete(payload),
                           "Dossier de stockage inaccessible : sonde refusée")
        self.dossier.mkdir.side_effect = None
        self.assert_refuse(self.requete(payload),
                           "Fuseau horaire inconnu : « Zone/Inexistante ».")
        self.contexte_sonde.__exit__.assert_called_once_with(None, None, None)

        payload["timezone"] = "UTC"
        self.assert_refuse(self.requete(payload),
                           "Protocole de direct inconnu : « inconnu ».")
        self.contexte_sonde.__exit__.assert_called_once_with(None, None, None)


if __name__ == "__main__":
    unittest.main()
