"""Caractérise la récupération après redémarrage du serveur web.

Un onglet ouvert conserve l'ancien jeton lorsque blink2video redémarre. Le
nouveau serveur répond alors avec la page HTML d'erreur de ``http.server`` ;
le vrai helper JavaScript doit recharger la page au lieu de laisser remonter
seulement ``Unexpected token '<'`` dans la console.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


class TestsLectureJSONApresRedemarrage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node introuvable")
        source = (Path(__file__).parent / "serve_app.js").read_text(encoding="utf-8")
        correspondance = re.search(
            r"async function lireJSON\(reponse\) \{.*?^\}",
            source,
            re.DOTALL | re.MULTILINE,
        )
        if correspondance is None:
            raise AssertionError("fonction lireJSON() introuvable dans serve_app.js")
        cls.fonction_lire_json = correspondance.group(0)

    def _executer(self, mode: str) -> dict:
        script = (
            "let rechargements = 0;\n"
            "globalThis.location = {reload: () => { rechargements += 1; }};\n"
            + self.fonction_lire_json
            + "\n(async () => {\n"
            + "  const mode = process.argv[1];\n"
            + "  const erreur = mode === 'html' ? new SyntaxError(\"Unexpected token '<'\")"
            + " : new TypeError('network');\n"
            + "  const reponse = {json: async () => {\n"
            + "    if (mode === 'ok') return {clips: 5};\n"
            + "    throw erreur;\n"
            + "  }};\n"
            + "  let resultat = null; let typeErreur = null;\n"
            + "  try { resultat = await lireJSON(reponse); }"
            + " catch (e) { typeErreur = e.constructor.name; }\n"
            + "  process.stdout.write(JSON.stringify({rechargements, resultat, typeErreur}));\n"
            + "})();\n"
        )
        resultat = subprocess.run(
            [self.node, "-e", script, mode],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return json.loads(resultat.stdout)

    def test_reponse_html_recharge_la_page_et_propage_lerreur(self):
        resultat = self._executer("html")
        self.assertEqual(resultat["rechargements"], 1)
        self.assertEqual(resultat["typeErreur"], "SyntaxError")

    def test_reponse_json_valide_ne_recharge_pas_la_page(self):
        resultat = self._executer("ok")
        self.assertEqual(resultat["rechargements"], 0)
        self.assertEqual(resultat["resultat"], {"clips": 5})
        self.assertIsNone(resultat["typeErreur"])

    def test_erreur_reseau_ne_provoque_pas_de_boucle_de_rechargement(self):
        resultat = self._executer("network")
        self.assertEqual(resultat["rechargements"], 0)
        self.assertEqual(resultat["typeErreur"], "TypeError")


if __name__ == "__main__":
    unittest.main(verbosity=2)
