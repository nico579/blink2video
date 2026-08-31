"""Vérifie que serve_app.js échappe correctement les noms de caméra affichés
(audit non daté, section « Améliorations ») : un nom contenant ', ", <, &
ne doit jamais casser le HTML ni s'exécuter comme script.

Exécute le VRAI code d'échappement expédié au navigateur via Node (déjà
présent sur les postes de développement et les runners GitHub Actions),
plutôt qu'une réimplémentation Python qui pourrait diverger silencieusement.
Sauté si node est introuvable.

L'identité de deux caméras homonymes (mêmes clés stables malgré un nom
partagé) est un problème séparé de l'échappement : voir
test_serve_security_audit.IdentiteCameraTests."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


class TestsEchappementNomsCamera(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node introuvable")
        source = (Path(__file__).parent / "serve_app.js").read_text(encoding="utf-8")
        correspondance = re.search(
            r"const h = \(value\) => .*?\}\)\[char\]\);", source, re.DOTALL,
        )
        if correspondance is None:
            raise AssertionError("fonction h() introuvable dans serve_app.js")
        cls.fonction_h = correspondance.group(0)

    def _echapper(self, *valeurs: str) -> list:
        script = (
            self.fonction_h
            + "\nconst entrees = JSON.parse(process.argv[1]);"
            + "\nprocess.stdout.write(JSON.stringify(entrees.map(h)));"
        )
        resultat = subprocess.run(
            [self.node, "-e", script, json.dumps(valeurs)],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return json.loads(resultat.stdout)

    def test_apostrophe_nest_jamais_un_guillemet_simple_brut(self):
        (echappe,) = self._echapper("Porte d'entrée")
        self.assertNotIn("'", echappe)
        self.assertIn("&#39;", echappe)

    def test_balise_script_est_neutralisee(self):
        (echappe,) = self._echapper("<script>alert(1)</script>")
        self.assertNotIn("<script>", echappe)
        self.assertIn("&lt;script&gt;", echappe)

    def test_esperluette_ne_devient_pas_le_debut_dune_autre_entite(self):
        (echappe,) = self._echapper("Jardin & Terrasse")
        self.assertIn("&amp;", echappe)
        self.assertNotIn(" & ", echappe)

    def test_guillemet_double_ne_peut_pas_casser_un_attribut_html(self):
        (echappe,) = self._echapper('" onmouseover="alert(1)')
        self.assertNotIn('"', echappe)
        self.assertIn("&quot;", echappe)

    def test_tous_les_caracteres_sensibles_a_la_fois(self):
        (echappe,) = self._echapper("""<a href="x" onclick='y'>&z</a>""")
        for brut in ("<", ">", '"', "'", "&z"):
            self.assertNotIn(brut, echappe)


if __name__ == "__main__":
    unittest.main(verbosity=2)
