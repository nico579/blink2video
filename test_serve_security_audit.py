"""Régressions des protections web et des identités de caméra."""

from __future__ import annotations

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
    def handler(self, client="127.0.0.1", host="127.0.0.1"):
        handler = serve.Handler.__new__(serve.Handler)
        handler.client_address = (client, 12345)
        handler.headers = {"Host": host}
        handler.path = "/api/status"
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
