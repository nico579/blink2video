"""Une sélection de directs ne peut pas sortir de sa racine autorisée."""

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"
_IMPORT_HOME = tempfile.TemporaryDirectory(prefix="blink-direct-scope-import-")
os.environ["BLINK_HOME"] = _IMPORT_HOME.name

import serve


class TestDirectConfinement(unittest.TestCase):
    def setUp(self):
        temporaire = tempfile.TemporaryDirectory(prefix="blink-direct-confinement-")
        self.addCleanup(temporaire.cleanup)
        self.racine = Path(temporaire.name)
        self.paths = {nom: self.racine / nom for nom in ("direct", "input", "thumbs")}
        for chemin in self.paths.values():
            chemin.mkdir()
        self.exterieur = self.racine / "temoin.mp4"
        self.exterieur.write_bytes(b"a conserver")

    def poster(self, payload):
        handler = serve.Handler.__new__(serve.Handler)
        handler.path = "/api/appliquer-selection"
        handler.paths = self.paths
        corps = json.dumps(payload).encode()
        handler.headers = {"Host": "127.0.0.1", "X-Blink-Token": serve.TOKEN,
                           "Content-Length": str(len(corps))}
        handler.rfile = io.BytesIO(corps)
        reponses = []
        handler.send_json = lambda payload, code=200: reponses.append((code, payload))
        with mock.patch.object(serve, "read_entries", return_value={}), \
                mock.patch.object(serve.BLINK, "call", side_effect=AssertionError("API interdite")):
            handler.do_POST()
        self.assertEqual(len(reponses), 1)
        return reponses[0]

    def test_traversees_refusees_pour_les_trois_operations(self):
        for operation in ("exclure", "inclure", "supprimer"):
            for identity in ("../temoin.mp4", "./temoin.mp4", ".. /temoin.mp4",
                             "dossier/../../temoin.mp4", "temoin.mp4\n"):
                with self.subTest(operation=operation, identity=identity):
                    code, resultat = self.poster({operation: [identity]})
                    self.assertEqual(code, 400)
                    self.assertIn("error", resultat)
                    self.assertEqual(self.exterieur.read_bytes(), b"a conserver")
        self.assertFalse((self.paths["thumbs"] / serve.DIRECT_EXCLUSION).exists())

    def test_lot_mixte_ne_supprime_pas_le_direct_valide(self):
        valide = self.paths["direct"] / "valide.mp4"
        valide.write_bytes(b"direct")
        code, _ = self.poster({"supprimer": ["valide.mp4", "../temoin.mp4"]})
        self.assertEqual(code, 400)
        self.assertTrue(valide.exists())
        self.assertTrue(self.exterieur.exists())

    def test_lien_sortant_est_refuse_avant_toute_suppression(self):
        lien = self.paths["direct"] / "lien.mp4"
        try:
            lien.symlink_to(self.exterieur)
        except OSError:
            self.skipTest("Création de liens symboliques non autorisée sur cet hôte")
        code, _ = self.poster({"supprimer": ["lien.mp4"]})
        self.assertEqual(code, 400)
        self.assertTrue(self.exterieur.exists())
        self.assertTrue(lien.is_symlink())

    def test_resolution_sortante_est_refusee_meme_sans_droits_de_creation_de_lien(self):
        resolver = Path.resolve
        # TEMP peut passer par un nom court Windows ou /var -> /private/var
        # sur macOS ; le serveur résout déjà la racine avant l'identité.
        alias = resolver(self.paths["direct"] / "alias.mp4")

        def resoudre(chemin, *args, **kwargs):
            normalise = resolver(chemin, *args, **kwargs)
            return resolver(self.exterieur) if normalise == alias else normalise

        with mock.patch.object(Path, "resolve", autospec=True, side_effect=resoudre):
            code, _ = self.poster({"supprimer": ["alias.mp4"]})
        self.assertEqual(code, 400)
        self.assertTrue(self.exterieur.exists())

    def test_direct_valide_supprime_sans_effacer_sa_racine(self):
        for identity in ("simple.mp4", "Salon/2026-09/direct.mp4"):
            with self.subTest(identity=identity):
                fichier = self.paths["direct"] / identity
                fichier.parent.mkdir(parents=True, exist_ok=True)
                fichier.write_bytes(b"direct")
                code, resultat = self.poster({"supprimer": [identity]})
                self.assertEqual(code, 200)
                self.assertEqual(resultat["resultats"][identity], "supprime")
                self.assertFalse(fichier.exists())
                self.assertTrue(self.paths["direct"].is_dir())
                self.assertTrue(self.exterieur.exists())

    def test_exclusion_et_reintegration_valides_ne_suppriment_pas(self):
        fichier = self.paths["direct"] / "direct.mp4"
        fichier.write_bytes(b"direct")
        self.assertEqual(self.poster({"exclure": ["direct.mp4"]})[0], 200)
        self.assertEqual(serve._lire_exclusion_directe(self.paths), {"direct.mp4"})
        self.assertEqual(self.poster({"inclure": ["direct.mp4"]})[0], 200)
        self.assertEqual(serve._lire_exclusion_directe(self.paths), set())
        self.assertTrue(fichier.exists())


if __name__ == "__main__":
    unittest.main()
