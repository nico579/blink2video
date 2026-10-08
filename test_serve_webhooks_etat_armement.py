"""Issue #40 : webhooks d'état et d'armement pour les scripts, avec le même
secret que la photo. Aucun appel réel à Blink : system_state() et set_armed()
sont simulés ; ce qui est testé, c'est le contrat HTTP, l'authentification, les
refus et le journal."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-webhooks-etat-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import runtime  # noqa: E402 - environnement isole avant import
import serve  # noqa: E402


def camera(nom, armed=True, offline=False, **champs):
    base = {
        "name": nom, "key": f"cle-{nom}", "armed": armed, "offline": offline,
        "status": "offline" if offline else "done", "battery": "ok", "battery_signal": 3,
        "voltage": 1.6, "temperature": 18.3, "wifi": 4, "firmware": "12.34", "model": "Blink Outdoor",
        "age_seconds": 120, "serial": "SECRET-SERIE", "lfr": 1, "measured_today": True,
    }
    base.update(champs)
    return base


def etat(*systemes):
    return {"systems": list(systemes), "passages": {}}


def systeme(nom, cameras, armed=True):
    return {"name": nom, "key": f"cle-{nom}", "armed": armed, "module": "Sync Module 2",
            "module_firmware": "9.9", "module_serial": "SERIE-MODULE", "cameras": cameras}


ETAT = etat(
    systeme("Maison", [camera("Salon"), camera("Cave", armed=False)]),
    systeme("Loft", [camera("Grenier", offline=True)], armed=False),
)


class ParametresEtFormes(unittest.TestCase):
    def test_booleens_acceptes(self):
        for v in ("true", "TRUE", " 1 ", "on", "Yes", "oui"):
            self.assertIs(serve._booleen_webhook(v), True, v)
        for v in ("false", "False", "0", "off", "no", "NON"):
            self.assertIs(serve._booleen_webhook(v), False, v)
        for v in ("", None, "peut-etre", "2", "vrai"):
            self.assertIsNone(serve._booleen_webhook(v), repr(v))

    def test_etat_complet_sans_numero_de_serie(self):
        r = serve.etat_webhook(ETAT)
        self.assertEqual([s["name"] for s in r["systems"]], ["Maison", "Loft"])
        salon = r["systems"][0]["cameras"][0]
        self.assertEqual(salon, {
            "name": "Salon", "system": "Maison", "armed": True, "online": True, "status": "done",
            "battery": "ok", "battery_signal": 3, "voltage": 1.6, "temperature_c": 18.3, "wifi": 4,
            "firmware": "12.34", "model": "Blink Outdoor", "age_seconds": 120})
        self.assertNotIn("SECRET-SERIE", str(r))
        self.assertNotIn("SERIE-MODULE", str(r))
        self.assertEqual(r["systems"][0]["module"], "Sync Module 2")
        self.assertEqual(r["systems"][0]["firmware"], "9.9")

    def test_une_camera_hors_ligne_est_signalee(self):
        grenier = serve.etat_webhook(ETAT)["systems"][1]["cameras"][0]
        self.assertFalse(grenier["online"])
        self.assertEqual(grenier["status"], "offline")

    def test_les_champs_absents_restent_nuls_sans_planter(self):
        # Une Mini sur secteur : ni batterie, ni tension, ni wifi, ni modele.
        mini = {"name": "Mini", "key": "k", "armed": True, "offline": False}
        r = serve.etat_webhook(etat(systeme("Maison", [mini])))["systems"][0]["cameras"][0]
        for champ in ("battery", "battery_signal", "voltage", "temperature_c", "wifi", "firmware",
                      "model", "age_seconds", "status"):
            self.assertIsNone(r[champ], champ)
        self.assertIs(r["online"], True)

    def test_une_seule_camera(self):
        r = serve.etat_webhook(ETAT, "Cave")
        self.assertEqual([s["name"] for s in r["systems"]], ["Maison"])
        self.assertEqual([c["name"] for c in r["systems"][0]["cameras"]], ["Cave"])

    def test_camera_inconnue_rend_none(self):
        self.assertIsNone(serve.etat_webhook(ETAT, "Fantome"))

    def test_etat_vide_est_valide(self):
        self.assertEqual(serve.etat_webhook({"systems": []}), {"systems": []})


class _Base(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory(prefix="blink-webhooks-")
        self.addCleanup(self.dossier.cleanup)
        self._ancien = os.environ.get("BLINK_HOME")
        os.environ["BLINK_HOME"] = self.dossier.name
        self.jeton = runtime.regenerer_jeton_webhook()
        self.h = serve.Handler.__new__(serve.Handler)
        self.h.send_json = mock.Mock()
        self.h.send_error = mock.Mock()
        self.h.system_state = mock.Mock(return_value=ETAT)
        self.h.set_armed = mock.Mock()
        self.h.command = "GET"
        self.h.headers = {}
        self.h.client_address = ("192.168.1.50", 40000)

    def tearDown(self):
        if self._ancien is None:
            os.environ.pop("BLINK_HOME", None)
        else:
            os.environ["BLINK_HOME"] = self._ancien

    def reponse(self):
        args = self.h.send_json.call_args
        return args.args[0], (args.args[1] if len(args.args) > 1 else 200)

    def journal(self):
        chemin = Path(self.dossier.name) / "armement-webhook.log"
        return chemin.read_text(encoding="utf-8") if chemin.exists() else ""


class WebhookStatus(_Base):
    def appeler(self, query):
        self.h.path = f"{serve.WEBHOOK_STATUS_ROUTE}?{query}"
        self.h.gerer_webhook_status()

    def test_etat_complet_avec_le_bon_secret(self):
        self.appeler(f"token={self.jeton}")
        corps, statut = self.reponse()
        self.assertEqual(statut, 200)
        self.assertEqual(len(corps["systems"]), 2)
        self.h.send_error.assert_not_called()

    def test_secret_absent_ou_faux_refuse_sans_lire_blink(self):
        for query in ("", "token=faux", "camera=Salon", f"token={self.jeton}x", "token=%C3%A9t%C3%A9"):
            with self.subTest(query=query):
                self.h.send_error.reset_mock()
                self.appeler(query)
                self.h.send_error.assert_called_once_with(403)
        self.h.system_state.assert_not_called()

    def test_une_camera(self):
        self.appeler(f"token={self.jeton}&camera=Cave")
        corps, _ = self.reponse()
        self.assertEqual(corps["systems"][0]["cameras"][0]["name"], "Cave")

    def test_camera_inconnue_404(self):
        self.appeler(f"token={self.jeton}&camera=Fantome")
        corps, statut = self.reponse()
        self.assertEqual(statut, 404)
        self.assertIn("Fantome", corps["error"])

    def test_blink_indisponible_503(self):
        self.h.system_state.side_effect = TimeoutError("trop long")
        self.appeler(f"token={self.jeton}")
        corps, statut = self.reponse()
        self.assertEqual(statut, 503)
        self.assertIn("TimeoutError", corps["error"])

    def test_etat_en_erreur_503(self):
        self.h.system_state.return_value = {"error": "pas de session"}
        self.appeler(f"token={self.jeton}")
        self.assertEqual(self.reponse(), ({"error": "pas de session"}, 503))


class WebhookArmement(_Base):
    def appeler(self, query, methode="GET", corps=b""):
        self.h.path = f"{serve.WEBHOOK_ARM_ROUTE}?{query}"
        self.h.command = methode
        self.h.rfile = io.BytesIO(corps)
        self.h.headers = {"Content-Length": str(len(corps))} if corps else {}
        self.h.gerer_webhook_arm()

    def apres(self, armed_salon):
        """Etat relu apres l'armement."""
        return etat(systeme("Maison", [camera("Salon", armed=armed_salon), camera("Cave", armed=False)]))

    def test_armer_une_camera(self):
        self.h.system_state.side_effect = [ETAT, self.apres(True)]
        self.appeler(f"camera=Cave&armed=true&token={self.jeton}")
        self.h.set_armed.assert_called_once_with("camera", "cle-Cave", True)
        corps, statut = self.reponse()
        self.assertEqual(statut, 200)
        self.assertTrue(corps["ok"])
        self.assertEqual(corps["scope"], "camera")
        self.assertIs(corps["requested"], True)

    def test_desarmer_et_dire_ce_que_blink_rapporte_juste_apres(self):
        # Blink n'a pas encore suivi : la camera apparait encore armee.
        self.h.system_state.side_effect = [ETAT, self.apres(True)]
        self.appeler(f"camera=Salon&armed=off&token={self.jeton}")
        self.h.set_armed.assert_called_once_with("camera", "cle-Salon", False)
        corps, _ = self.reponse()
        self.assertIs(corps["requested"], False)
        self.assertIs(corps["applied"], False)
        self.assertIs(corps["camera"]["armed"], True)

    def test_applique_quand_blink_a_suivi(self):
        self.h.system_state.side_effect = [ETAT, self.apres(False)]
        self.appeler(f"camera=Salon&armed=0&token={self.jeton}")
        self.assertIs(self.reponse()[0]["applied"], True)

    def test_armer_un_systeme(self):
        apres = etat(systeme("Maison", [camera("Salon")], armed=False), systeme("Loft", [], armed=True))
        self.h.system_state.side_effect = [ETAT, apres]
        self.appeler(f"system=Loft&armed=true&token={self.jeton}")
        self.h.set_armed.assert_called_once_with("system", "cle-Loft", True)
        corps, _ = self.reponse()
        self.assertEqual(corps["scope"], "system")
        self.assertEqual(corps["system"], {"name": "Loft", "armed": True})
        self.assertIs(corps["applied"], True)

    def test_changed_dit_si_l_etat_a_vraiment_change(self):
        # Retour de Markus (issue #40) : une camera deja desarmee rendait « applied: true »
        # sans dire qu'il n'y avait rien eu a faire.
        self.h.system_state.side_effect = [ETAT, self.apres(True)]
        self.appeler(f"camera=Cave&armed=false&token={self.jeton}")      # Cave deja desarmee
        self.assertIs(self.reponse()[0]["changed"], False)
        self.h.system_state.side_effect = [ETAT, self.apres(True)]
        self.appeler(f"camera=Cave&armed=true&token={self.jeton}")
        self.assertIs(self.reponse()[0]["changed"], True)
        self.h.system_state.side_effect = [ETAT, ETAT]
        self.appeler(f"system=Maison&armed=true&token={self.jeton}")       # Maison deja armee
        self.assertIs(self.reponse()[0]["changed"], False)

    def test_plusieurs_cameras_en_un_appel(self):
        apres = etat(systeme("Maison", [camera("Salon", armed=False), camera("Cave", armed=False)]))
        self.h.system_state.side_effect = [ETAT, apres, apres]
        self.appeler(f"camera=Salon&camera=Cave&armed=off&token={self.jeton}")
        corps, statut = self.reponse()
        self.assertEqual(statut, 200)
        self.assertEqual(corps["scope"], "cameras")
        self.assertIs(corps["ok"], True)
        self.assertEqual([r["name"] for r in corps["cameras"]], ["Salon", "Cave"])
        salon, cave = corps["cameras"]
        self.assertEqual((salon["ok"], salon["changed"], salon["applied"]), (True, True, True))
        self.assertEqual((cave["ok"], cave["changed"], cave["applied"]), (True, False, True))
        self.assertEqual(self.h.set_armed.call_args_list,
                         [mock.call("camera", "cle-Salon", False), mock.call("camera", "cle-Cave", False)])
        self.assertEqual(self.journal().count("armed=false"), 2)

    def test_une_camera_hors_ligne_ou_inconnue_n_arrete_pas_les_autres(self):
        self.h.system_state.side_effect = [ETAT, self.apres(True)]
        self.appeler(f"camera=Grenier&camera=Inconnue&camera=Cave&armed=on&token={self.jeton}")
        corps, statut = self.reponse()
        self.assertEqual(statut, 200)
        self.assertIs(corps["ok"], False)                  # pas toutes reussies
        resultats = {r["name"]: r for r in corps["cameras"]}
        self.assertEqual(resultats["Grenier"]["status_code"], 409)
        self.assertIs(resultats["Grenier"]["ok"], False)
        self.assertIn("camera", resultats["Grenier"])      # son etat, pour reagir
        self.assertEqual(resultats["Inconnue"]["status_code"], 404)
        self.assertIs(resultats["Cave"]["ok"], True)
        self.h.set_armed.assert_called_once_with("camera", "cle-Cave", True)

    def test_une_camera_repetee_n_est_armee_qu_une_fois(self):
        self.h.system_state.side_effect = [ETAT, self.apres(True)]
        self.appeler(f"camera=Cave&camera=Cave&armed=on&token={self.jeton}")
        corps, _ = self.reponse()
        self.assertEqual(corps["scope"], "camera")         # une seule cible : forme simple
        self.h.set_armed.assert_called_once()

    def test_cameras_et_systeme_ensemble_refuses(self):
        self.appeler(f"camera=Salon&camera=Cave&system=Maison&armed=on&token={self.jeton}")
        self.assertEqual(self.reponse()[1], 400)
        self.h.set_armed.assert_not_called()

    def test_secret_absent_ou_faux_refuse_sans_toucher_a_blink(self):
        for query in ("camera=Salon&armed=true", "camera=Salon&armed=true&token=faux",
                      f"camera=Salon&armed=true&token={self.jeton}x"):
            with self.subTest(query=query):
                self.h.send_error.reset_mock()
                self.appeler(query)
                self.h.send_error.assert_called_once_with(403)
        self.h.set_armed.assert_not_called()
        self.h.system_state.assert_not_called()
        self.assertEqual(self.journal(), "")

    def test_il_faut_exactement_une_cible(self):
        for cible in ("", "camera=Salon&system=Maison"):
            with self.subTest(cible=cible):
                self.appeler(f"{cible}&armed=true&token={self.jeton}")
                self.assertEqual(self.reponse()[1], 400)
        self.h.set_armed.assert_not_called()

    def test_armed_obligatoire_et_valide(self):
        for valeur in ("", "&armed=", "&armed=peut-etre", "&armed=2"):
            with self.subTest(valeur=valeur):
                self.appeler(f"camera=Salon{valeur}&token={self.jeton}")
                self.assertEqual(self.reponse()[1], 400)
        self.h.set_armed.assert_not_called()

    def test_camera_ou_systeme_inconnu_404(self):
        self.appeler(f"camera=Fantome&armed=true&token={self.jeton}")
        self.assertEqual(self.reponse()[1], 404)
        self.appeler(f"system=Fantome&armed=true&token={self.jeton}")
        self.assertEqual(self.reponse()[1], 404)
        self.h.set_armed.assert_not_called()

    def test_nom_porte_par_deux_cameras_409(self):
        double = etat(systeme("Maison", [camera("Porte")]), systeme("Loft", [camera("Porte")]))
        self.h.system_state.return_value = double
        self.appeler(f"camera=Porte&armed=true&token={self.jeton}")
        corps, statut = self.reponse()
        self.assertEqual(statut, 409)
        self.assertIn("Porte", corps["error"])
        self.h.set_armed.assert_not_called()

    def test_camera_hors_ligne_n_est_pas_tentee(self):
        self.appeler(f"camera=Grenier&armed=true&token={self.jeton}")
        corps, statut = self.reponse()
        self.assertEqual(statut, 409)
        self.assertIn("hors ligne", corps["error"])
        self.assertIs(corps["camera"]["online"], False)
        self.h.set_armed.assert_not_called()
        self.assertEqual(self.journal(), "")

    def test_refus_de_blink_503(self):
        self.h.set_armed.side_effect = RuntimeError("Caméra inconnue : x")
        self.appeler(f"camera=Salon&armed=true&token={self.jeton}")
        self.assertEqual(self.reponse(), ({"error": "Caméra inconnue : x"}, 503))
        self.assertEqual(self.journal(), "", "rien n'a ete arme : rien a journaliser")

    def test_delai_depasse_503(self):
        self.h.set_armed.side_effect = TimeoutError("60 s")
        self.appeler(f"camera=Salon&armed=true&token={self.jeton}")
        corps, statut = self.reponse()
        self.assertEqual(statut, 503)
        self.assertIn("TimeoutError", corps["error"])

    def test_le_changement_est_journalise_sans_le_secret(self):
        self.h.system_state.side_effect = [ETAT, self.apres(False)]
        self.appeler(f"camera=Salon&armed=false&token={self.jeton}")
        ligne = self.journal()
        self.assertIn("camera", ligne)
        self.assertIn("Salon", ligne)
        self.assertIn("armed=false", ligne)
        self.assertIn("192.168.1.50", ligne)
        self.assertNotIn(self.jeton, ligne)

    def test_la_relecture_apres_armement_qui_echoue_ne_cache_pas_le_succes(self):
        self.h.system_state.side_effect = [ETAT, TimeoutError("relecture")]
        self.appeler(f"camera=Salon&armed=false&token={self.jeton}")
        corps, statut = self.reponse()
        self.assertEqual(statut, 200)
        self.assertTrue(corps["ok"])
        self.assertNotIn("applied", corps)

    def test_post_accepte_et_vide_son_corps(self):
        self.h.system_state.side_effect = [ETAT, self.apres(False)]
        corps = b'{"ignore": "tout passe par l\'URL"}'
        self.appeler(f"camera=Salon&armed=false&token={self.jeton}", methode="POST", corps=corps)
        self.assertEqual(self.h.rfile.tell(), len(corps), "le corps doit etre lu, pas laisse sur la connexion")
        self.assertTrue(self.reponse()[0]["ok"])

    def test_post_au_corps_enorme_ferme_la_connexion_sans_le_lire(self):
        self.h.close_connection = False
        self.h.system_state.side_effect = [ETAT, self.apres(False)]
        self.h.path = f"{serve.WEBHOOK_ARM_ROUTE}?camera=Salon&armed=false&token={self.jeton}"
        self.h.command = "POST"
        self.h.rfile = io.BytesIO(b"x" * 10)
        self.h.headers = {"Content-Length": str(serve._CORPS_WEBHOOK_MAX + 1)}
        self.h.gerer_webhook_arm()
        self.assertTrue(self.h.close_connection)
        self.assertEqual(self.h.rfile.tell(), 0)


class Routage(_Base):
    def test_do_get_route_les_deux_webhooks_avant_les_gardes_du_navigateur(self):
        for route, gestionnaire in ((serve.WEBHOOK_STATUS_ROUTE, "gerer_webhook_status"),
                                    (serve.WEBHOOK_ARM_ROUTE, "gerer_webhook_arm")):
            with self.subTest(route=route):
                self.h.path = f"{route}?token={self.jeton}"
                self.h.hote_autorise = mock.Mock(return_value=False)
                self.h.jeton_valide = mock.Mock(return_value=False)
                setattr(self.h, gestionnaire, mock.Mock())
                serve.Handler.do_GET(self.h)
                getattr(self.h, gestionnaire).assert_called_once()
                self.h.hote_autorise.assert_not_called()
                self.h.jeton_valide.assert_not_called()

    def test_do_post_route_l_armement_avant_les_gardes_du_navigateur(self):
        self.h.path = f"{serve.WEBHOOK_ARM_ROUTE}?token={self.jeton}"
        self.h.hote_autorise = mock.Mock(return_value=False)
        self.h.jeton_valide = mock.Mock(return_value=False)
        self.h.gerer_webhook_arm = mock.Mock()
        serve.Handler.do_POST(self.h)
        self.h.gerer_webhook_arm.assert_called_once()
        self.h.hote_autorise.assert_not_called()
        self.h.jeton_valide.assert_not_called()

    def test_un_autre_post_garde_ses_gardes_du_navigateur(self):
        self.h.path = "/api/arm"
        self.h.hote_autorise = mock.Mock(return_value=False)
        self.h.jeton_valide = mock.Mock(return_value=False)
        self.h._refuser = mock.Mock()
        self.h.gerer_webhook_arm = mock.Mock()
        serve.Handler.do_POST(self.h)
        self.h.gerer_webhook_arm.assert_not_called()
        self.h._refuser.assert_called_once_with(403)

    def test_status_n_accepte_pas_le_post_sans_session(self):
        self.h.path = f"{serve.WEBHOOK_STATUS_ROUTE}?token={self.jeton}"
        self.h.hote_autorise = mock.Mock(return_value=False)
        self.h.jeton_valide = mock.Mock(return_value=False)
        self.h._refuser = mock.Mock()
        serve.Handler.do_POST(self.h)
        self.h._refuser.assert_called_once_with(403)


class Messages(unittest.TestCase):
    def test_les_nouveaux_textes_existent_dans_les_deux_langues(self):
        for langue in ("fr", "en"):
            with mock.patch.object(runtime, "lire_langue", return_value=langue):
                for cle, valeurs in (
                    ("webhook_camera_inconnue", {"camera": "X"}), ("webhook_systeme_inconnu", {"systeme": "X"}),
                    ("webhook_nom_ambigu", {"nom": "X"}), ("webhook_cible", {}),
                    ("webhook_armed_invalide", {}), ("webhook_camera_hors_ligne", {"camera": "X"}),
                ):
                    with self.subTest(langue=langue, cle=cle):
                        texte = serve.msg(cle, **valeurs)
                        self.assertNotIn("{", texte)
                        self.assertTrue(texte.strip())


if __name__ == "__main__":
    unittest.main()
