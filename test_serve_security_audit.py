"""Régressions des protections web et des identités de caméra."""

from __future__ import annotations

import http.server
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-security-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import serve  # noqa: E402 - environnement isolé avant import


class SecuriteWebTests(unittest.TestCase):
    def handler(self, client="127.0.0.1", host="127.0.0.1", trusted_host=""):
        handler = serve.Handler.__new__(serve.Handler)
        handler.client_address = (client, 12345)
        handler.headers = {"Host": host}
        handler.path = "/api/status"
        handler.trusted_host = trusted_host
        return handler

    def test_host_local_ne_suffit_pas_a_un_client_distant(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BLINK_TRUSTED_LOOPBACK_PROXY", None)
            self.assertFalse(self.handler("192.0.2.4").hote_autorise())
            self.assertTrue(self.handler().hote_autorise())

    def test_proxy_docker_exige_un_opt_in_et_un_host_loopback(self):
        with mock.patch.dict(os.environ, {"BLINK_TRUSTED_LOOPBACK_PROXY": "1"}):
            self.assertTrue(self.handler("172.18.0.1").hote_autorise())
            self.assertFalse(
                self.handler("172.18.0.1", "192.168.1.20").hote_autorise()
            )

    def test_hote_de_confiance_sans_opt_in_reste_refuse(self):
        self.assertFalse(
            self.handler("100.101.194.5", "100.101.194.5").hote_autorise()
        )

    def test_refus_journalise_la_raison_dans_serve_erreurs_log(self):
        # Issue #10 : un trusted_host mal renseigné ne laissait qu'un 403
        # générique côté navigateur, sans rien d'exploitable pour diagnostiquer
        # à distance. hote_autorise() doit tracer pourquoi, dans le même
        # fichier que handle_error.
        import runtime as rt
        journal = rt.app_dir() / "serve_erreurs.log"
        journal.unlink(missing_ok=True)
        self.assertFalse(
            self.handler("100.101.194.5", "100.101.194.5").hote_autorise()
        )
        self.assertTrue(journal.exists())
        contenu = journal.read_text(encoding="utf-8")
        self.assertIn("100.101.194.5", contenu)
        self.assertIn("accès refusé", contenu)

    def test_hote_de_confiance_accepte_un_client_distant_qui_le_designe(self):
        self.assertTrue(
            self.handler("100.101.194.5", "100.101.194.5",
                        trusted_host="100.101.194.5").hote_autorise()
        )
        # Un autre client distant, même Host : accepté aussi, la garantie
        # vient du réseau du tunnel, pas de quel appareil précis y est.
        self.assertTrue(
            self.handler("100.64.9.9", "100.101.194.5",
                        trusted_host="100.101.194.5").hote_autorise()
        )

    def test_hote_de_confiance_cidr_accepte_toute_adresse_du_sous_reseau(self):
        # Issue #10 : client Windows en DHCP, pas d'adresse fixe a mettre
        # dans un champ IP unique - un sous-reseau entier plutot qu'une IP.
        for ip in ("192.168.1.1", "192.168.1.254"):
            with self.subTest(ip=ip):
                self.assertTrue(
                    self.handler(ip, ip, trusted_host="192.168.1.0/24").hote_autorise()
                )

    def test_hote_de_confiance_cidr_refuse_hors_du_sous_reseau(self):
        self.assertFalse(
            self.handler("192.168.2.5", "192.168.2.5",
                        trusted_host="192.168.1.0/24").hote_autorise()
        )

    def test_hote_de_confiance_cidr_malforme_refuse_sans_lever(self):
        # trusted_host est un reglage persiste : une valeur corrompue sur
        # disque (edition manuelle du fichier de reglages, vieux format)
        # ne doit jamais faire planter la verification, seulement l'echouer.
        self.assertFalse(
            self.handler("192.168.1.1", "192.168.1.1",
                        trusted_host="not/a/cidr").hote_autorise()
        )

    def test_hote_de_confiance_n_elargit_pas_a_un_host_different(self):
        self.assertFalse(
            self.handler("100.101.194.5", "un-autre-host.example",
                        trusted_host="100.101.194.5").hote_autorise()
        )

    def test_hote_de_confiance_toujours_verifie_via_origin(self):
        handler = self.handler("100.101.194.5", "100.101.194.5",
                               trusted_host="100.101.194.5")
        handler.headers["Origin"] = "http://100.101.194.5"
        self.assertTrue(handler.hote_autorise())
        handler.headers["Origin"] = "http://attaquant.example"
        self.assertFalse(handler.hote_autorise())

    def test_origin_malformee_refusee_sans_lever(self):
        # urlparse lève ValueError sur certaines formes manifestement
        # invalides (IPv6 mal fermé) plutôt que de rendre un hostname vide :
        # confirmé en direct contre une vraie instance (curl avec cet en-tête
        # provoquait une exception non rattrapée jusqu'à do_GET, connexion
        # coupée au lieu d'un 403 propre).
        handler = self.handler()
        handler.headers["Origin"] = "http://[invalid"
        self.assertFalse(handler.hote_autorise())

    def test_jeton_est_accepte_en_entete_ou_dans_url_media(self):
        handler = self.handler()
        handler.headers["X-Blink-Token"] = serve.TOKEN
        self.assertTrue(handler.jeton_valide())
        del handler.headers["X-Blink-Token"]
        handler.path = f"/media/clip/test.mp4?token={serve.TOKEN}"
        self.assertTrue(handler.jeton_valide())

    def test_get_sensible_sans_jeton_est_refuse(self):
        erreurs = []
        handler = SimpleNamespace(
            path="/api/choisir-dossier",
            hote_autorise=lambda: True,
            jeton_valide=lambda: False,
            send_error=lambda code, *_args: erreurs.append(code),
        )
        serve.Handler.do_GET(handler)
        self.assertEqual(erreurs, [403])

    def test_page_n_injecte_plus_de_handlers_inline(self):
        self.assertNotIn("onclick=", serve.PAGE)
        self.assertNotIn("onchange=", serve.PAGE)
        self.assertIn(f'<script nonce="{serve.SCRIPT_NONCE}">', serve.PAGE)
        self.assertIn("const h =", serve.PAGE)

    def _frame_ancestors(self, trusted_host: str) -> str:
        handler = self.handler(trusted_host=trusted_host)
        handler.send_header = mock.Mock()
        with mock.patch.object(http.server.BaseHTTPRequestHandler, "end_headers"):
            handler.end_headers()
        (nom, valeur), = (
            appel.args for appel in handler.send_header.call_args_list
            if appel.args[0] == "Content-Security-Policy"
        )
        return next(
            directive for directive in valeur.split("; ")
            if directive.startswith("frame-ancestors")
        )

    def test_frame_ancestors_none_sans_hote_de_confiance(self):
        self.assertEqual(self._frame_ancestors(""), "frame-ancestors 'none'")

    def test_frame_ancestors_autorise_l_hote_de_confiance_exact(self):
        # Issue #12 : embarquer la page dans un tableau de bord domotique
        # (ioBroker en iframe). Sur le meme hote que trusted_host puisque
        # c'est deja la garantie de securite en place pour l'acces direct.
        self.assertEqual(
            self._frame_ancestors("192.168.201.253"),
            "frame-ancestors 'self' 192.168.201.253")

    def test_frame_ancestors_reste_none_pour_un_sous_reseau_cidr(self):
        # frame-ancestors n'a pas de syntaxe pour un sous-reseau : accepter
        # un CIDR ici donnerait une fausse impression de protection.
        self.assertEqual(
            self._frame_ancestors("192.168.1.0/24"), "frame-ancestors 'none'")

    # Issue #13 : trusted_host en liste, pour meler iframe et acces direct.

    def test_liste_accepte_chacune_de_ses_entrees(self):
        liste = "192.168.1.10,192.168.201.0/24,server"
        for client, host in (("192.168.1.10", "192.168.1.10"),
                             ("192.168.201.134", "192.168.201.134"),
                             ("192.168.201.134", "server")):
            with self.subTest(host=host):
                self.assertTrue(
                    self.handler(client, host, trusted_host=liste).hote_autorise())

    def test_liste_n_elargit_pas_hors_de_ses_entrees(self):
        # Un nom n'est jamais couvert par un sous-reseau : le navigateur envoie
        # le nom tape comme Host, pas l'IP qu'il designe.
        liste = "192.168.1.10,192.168.201.0/24"
        for client, host in (("192.168.2.5", "192.168.2.5"),
                             ("192.168.201.134", "server")):
            with self.subTest(host=host):
                self.assertFalse(
                    self.handler(client, host, trusted_host=liste).hote_autorise())

    def test_frame_ancestors_liste_ne_garde_que_les_entrees_exactes(self):
        self.assertEqual(
            self._frame_ancestors("192.168.1.10,192.168.201.0/24,server"),
            "frame-ancestors 'self' 192.168.1.10 server")

    def test_frame_ancestors_ignore_une_entree_invalide_d_un_reglage_edite_a_la_main(self):
        # La page de reglages refuse deja ce « ; », mais un fichier de reglages
        # modifie a la main n'y passe pas : il ne doit rien ajouter a l'en-tete.
        self.assertEqual(
            self._frame_ancestors("iobroker;script-src,192.168.1.10"),
            "frame-ancestors 'self' 192.168.1.10")


class IdentiteCameraTests(unittest.TestCase):
    def test_deux_cameras_homonymes_de_reseaux_distincts_ont_deux_cles(self):
        camera_a = SimpleNamespace(attributes={}, network_id="1")
        camera_b = SimpleNamespace(attributes={}, network_id="2")
        sync_a = SimpleNamespace(network_id="1", sync_id="10")
        sync_b = SimpleNamespace(network_id="2", sync_id="20")
        self.assertNotEqual(
            serve.camera_key(sync_a, "Jardin", camera_a),
            serve.camera_key(sync_b, "Jardin", camera_b),
        )

    def test_suppression_auto_distingue_les_homonymes(self):
        entries = {
            "a": {"camera": "Jardin", "network_id": "1", "sync_id": "10",
                  "source": "usb", "hub": "Maison"},
            "b": {"camera": "Jardin", "network_id": "2", "sync_id": "20",
                  "source": "usb", "hub": "Atelier"},
        }
        choices = serve.suppression_auto_choices(entries)
        self.assertEqual(len(choices), 2)
        self.assertEqual(len({choice["key"] for choice in choices}), 2)


if __name__ == "__main__":
    unittest.main()
