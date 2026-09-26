"""Le routage POST conserve les protections communes et les corps métier."""

import http.server
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock


_IMPORT_HOME = tempfile.TemporaryDirectory(prefix="blink-post-routage-import-")
with mock.patch.dict(os.environ, {"BLINK_BOOTSTRAP": "none",
                                  "BLINK_HOME": _IMPORT_HOME.name}):
    import serve


ROUTES_REGLAGES = {
    "/api/reglages": "_post_reglages",
    "/api/sourdine": "_post_sourdine",
    "/api/suppression-auto": "_post_suppression_auto",
}
ROUTES = {
    **ROUTES_REGLAGES,
    "/api/appliquer-selection": "_post_appliquer_selection",
}


class TestsRoutagePost(unittest.TestCase):
    def setUp(self):
        # Aucun de ces tests ne doit écrire, contacter Blink ou lancer un
        # travail en arrière-plan, même si le routage régresse.
        self.mutations = []
        for module, nom in (
            (serve.runtime, "ecrire_dossier_stockage"),
            (serve.runtime, "ecrire_suppression_auto"),
            (serve.runtime, "lancer"),
            (serve.runtime, "demarrer"),
            (serve.threading, "Thread"),
            (serve.BLINK, "call"),
            (serve.md, "set_excluded"),
            (serve.blink_registre, "save_download_state"),
            (serve, "_ecrire_exclusion_directe"),
        ):
            patch = mock.patch.object(
                module, nom, side_effect=AssertionError("Mutation interdite : " + nom))
            self.mutations.append(patch.start())
            self.addCleanup(patch.stop)

    def tearDown(self):
        for mutation in self.mutations:
            mutation.assert_not_called()

    def handler(self, route, corps=b"{}", isoler_metier=True):
        handler = object.__new__(serve.Handler)
        handler.path = route
        handler.paths = {}
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {
            "Host": "127.0.0.1:8765",
            "X-Blink-Token": serve.TOKEN,
            "Content-Length": str(len(corps)),
        }
        handler.rfile = mock.Mock(wraps=io.BytesIO(corps))
        handler.send_json = mock.Mock()
        handler.send_error = mock.Mock()
        if isoler_metier:
            for methode in ROUTES.values():
                setattr(handler, methode, mock.Mock(name=methode))
        return handler

    def assert_aucun_traitement(self, handler):
        for methode in ROUTES.values():
            getattr(handler, methode).assert_not_called()

    def assert_route(self, handler, route, payload):
        methode = ROUTES[route]
        resultat = handler.do_POST()
        appele = getattr(handler, methode)
        appele.assert_called_once_with(payload)
        self.assertIs(resultat, appele.return_value)
        for autre in ROUTES.values():
            if autre != methode:
                getattr(handler, autre).assert_not_called()
        handler.send_error.assert_not_called()
        handler.send_json.assert_not_called()
        handler.rfile.read.assert_called_once_with(
            int(handler.headers.get("Content-Length") or 0))

    # Un refus lit le corps annoncé, borné, sans jamais l'interpréter :
    # fermer la connexion sur des octets non lus faisait perdre la réponse
    # sous Windows (RST). Rien n'est traité d'un client non autorisé.
    def assert_corps_draine_sans_traitement(self, handler):
        handler.send_error.assert_called_once_with(403)
        handler.send_json.assert_not_called()
        handler.rfile.read.assert_called_once_with(
            int(handler.headers["Content-Length"]))
        self.assert_aucun_traitement(handler)

    def test_hote_ou_origine_refuse_sans_traitement(self):
        for route in ROUTES:
            for entete, valeur in (("Host", "externe.invalid:8765"),
                                   ("Origin", "https://externe.invalid")):
                with self.subTest(route=route, entete=entete):
                    handler = self.handler(route)
                    handler.headers[entete] = valeur
                    handler.do_POST()
                    self.assert_corps_draine_sans_traitement(handler)

    def test_jeton_absent_ou_incorrect_refuse_sans_traitement(self):
        for route in ROUTES:
            for token in (None, "jeton-incorrect"):
                with self.subTest(route=route, token=token):
                    handler = self.handler(route)
                    if token is None:
                        del handler.headers["X-Blink-Token"]
                    else:
                        handler.headers["X-Blink-Token"] = token
                    handler.do_POST()
                    self.assert_corps_draine_sans_traitement(handler)

    def test_refus_ne_lit_jamais_un_corps_annonce_trop_gros(self):
        # Au-delà d'1 Mo, la connexion est fermée sans lire : un client non
        # autorisé ne peut pas occuper le serveur avec un envoi démesuré.
        handler = self.handler("/api/reglages")
        handler.headers["X-Blink-Token"] = "jeton-incorrect"
        handler.headers["Content-Length"] = str(2 * 1024 * 1024)
        handler.do_POST()
        handler.send_error.assert_called_once_with(403)
        handler.rfile.read.assert_not_called()
        self.assert_aucun_traitement(handler)

    def test_json_malforme_refuse_avant_traitement(self):
        for route in ROUTES:
            with self.subTest(route=route):
                handler = self.handler(route, b'{"camera":')
                handler.do_POST()
                handler.send_json.assert_called_once_with(
                    {"error": "corps JSON illisible"}, 400)
                handler.send_error.assert_not_called()
                self.assert_aucun_traitement(handler)

    def test_json_valide_mais_pas_un_objet_refuse_avant_traitement(self):
        for route in ROUTES:
            for corps in (b"[]", b"42", b'"texte"', b"null", b"true"):
                with self.subTest(route=route, corps=corps):
                    handler = self.handler(route, corps)
                    handler.do_POST()
                    handler.send_json.assert_called_once_with(
                        {"error": "corps JSON : objet attendu"}, 400)
                    handler.send_error.assert_not_called()
                    self.assert_aucun_traitement(handler)

    def test_octets_non_utf8_refuses_comme_json_illisible(self):
        handler = self.handler("/api/reglages", b'{"camera":"\xff\xfe"}')
        handler.do_POST()
        handler.send_json.assert_called_once_with({"error": "corps JSON illisible"}, 400)
        self.assert_aucun_traitement(handler)

    def test_longueur_negative_refusee_sans_lecture_bloquante(self):
        handler = self.handler("/api/reglages", b"{}")
        handler.headers["Content-Length"] = "-1"
        handler.do_POST()
        handler.send_error.assert_called_once_with(400)
        handler.rfile.read.assert_not_called()
        self.assert_aucun_traitement(handler)

    def test_chaque_route_transmet_uniquement_son_payload(self):
        payload = {"camera": "Entrée [réseau 1, appareil 2]", "actif": False,
                   "reglages": {"port": 8765}, "liste": [1, None, True]}
        corps = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for route in ROUTES:
            with self.subTest(route=route):
                self.assert_route(self.handler(route, corps), route, payload)

    def test_query_string_et_jeton_url_restent_acceptes(self):
        for route in ROUTES:
            with self.subTest(route=route):
                chemin = route + "?option=conservee&token=" + serve.TOKEN
                handler = self.handler(chemin, b'{"camera":"Salon"}')
                del handler.headers["X-Blink-Token"]
                self.assert_route(handler, route, {"camera": "Salon"})
                self.assertEqual(handler.path, chemin)

    def test_corps_vide_et_longueur_absente_donnent_un_objet_vide(self):
        for route in ROUTES:
            with self.subTest(route=route):
                handler = self.handler(route, b"")
                del handler.headers["Content-Length"]
                self.assert_route(handler, route, {})

    def test_routes_ne_sont_pas_des_prefixes(self):
        for route in ROUTES:
            for suffixe in ("/", "/autre", "-autre"):
                with self.subTest(route=route, suffixe=suffixe):
                    handler = self.handler(route + suffixe)
                    handler.do_POST()
                    handler.send_error.assert_called_once_with(404)
                    handler.send_json.assert_not_called()
                    self.assert_aucun_traitement(handler)

    def test_route_inconnue_repond_404(self):
        handler = self.handler("/api/route-inconnue")
        handler.do_POST()
        handler.send_error.assert_called_once_with(404)
        handler.send_json.assert_not_called()
        self.assert_aucun_traitement(handler)

    def test_validateurs_reels_conservent_le_refus_du_payload_vide(self):
        for route in ROUTES_REGLAGES:
            with self.subTest(route=route):
                handler = self.handler(route, isoler_metier=False)
                # La suppression automatique lit l'état sous verrou avant
                # de refuser une caméra absente ; seule cette lecture est simulée.
                with mock.patch.object(serve.runtime, "verrou_configuration"), \
                        mock.patch.object(serve.md, "load_json", return_value={}):
                    handler.do_POST()
                handler.send_error.assert_not_called()
                handler.send_json.assert_called_once()
                payload, code = handler.send_json.call_args[0]
                self.assertEqual(code, 400)
                self.assertIn("error", payload)

    def test_selection_vide_reste_valide_sans_mutation(self):
        for payload in ({}, {"exclure": [], "inclure": [], "supprimer": []}):
            with self.subTest(payload=payload):
                handler = self.handler(
                    "/api/appliquer-selection", json.dumps(payload).encode("utf-8"),
                    isoler_metier=False)
                with mock.patch.object(serve, "read_entries", return_value={}) as lire:
                    handler.do_POST()
                lire.assert_called_once_with(handler.paths)
                handler.send_json.assert_called_once_with({"ok": True, "resultats": {}})
                handler.send_error.assert_not_called()

    def test_selection_malformee_refusee_avant_lecture_et_mutation(self):
        for champ in ("exclure", "inclure", "supprimer"):
            for valeur in ("clip.mp4", {"clip": "clip.mp4"}, 1, [None], [1],
                           ["../clip.mp4"], ["camera/mois/clip.mp4\n"]):
                with self.subTest(champ=champ, valeur=valeur):
                    handler = self.handler(
                        "/api/appliquer-selection",
                        json.dumps({champ: valeur}).encode("utf-8"),
                        isoler_metier=False)
                    with mock.patch.object(serve, "read_entries") as lire:
                        handler.do_POST()
                    lire.assert_not_called()
                    handler.send_json.assert_called_once_with(
                        {"error": "Sélection de vidéos invalide."}, 400)
                    handler.send_error.assert_not_called()


class TestsRefusSurVraieConnexion(unittest.TestCase):
    """Le vrai handler derrière un serveur local : un refus de jeton doit
    toujours arriver au client. Fermée sur un corps non lu, la connexion
    partait en RST sous Windows (25 réponses 403 perdues sur 300, mesuré le
    2026-09-26) ; cinquante envois rendent la course presque certaine."""

    def setUp(self):
        for correctif in (mock.patch.object(serve.Handler, "trusted_host", ""),
                          mock.patch.object(serve.Handler, "log_message",
                                            lambda *args: None)):
            correctif.start()
            self.addCleanup(correctif.stop)
        self.serveur = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.addCleanup(self.serveur.server_close)
        self.addCleanup(self.serveur.shutdown)
        threading.Thread(target=self.serveur.serve_forever, daemon=True).start()

    def test_refus_de_jeton_arrive_toujours_au_client(self):
        port = self.serveur.server_address[1]
        corps = json.dumps({"donnees": "x" * 20000}).encode("utf-8")
        for _ in range(50):
            requete = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/reglages", data=corps, method="POST",
                headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(requete, timeout=10)
            self.assertEqual(ctx.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
