"""Bug rapporté sur GitHub (issue #6, 2026-09-13) : le panneau de journal
d'Actualiser affichait le texte littéral « \\n » entre chaque ligne au lieu
d'un vrai saut de ligne. Cause : `$("log").textContent += event.line + "\\n"`
dans le gestionnaire `source.onmessage` (flux SSE de /api/refresh) - la barre
oblique inversée était elle-même échappée dans le code source, ce qui produit
la chaîne à deux caractères `\\n` au lieu du caractère de saut de ligne que
`#log { white-space: pre-wrap }` (serve_style.css) sait pourtant déjà
afficher correctement."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


class TestJournalSautDeLigne(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node introuvable")
        source = (Path(__file__).parent / "serve_app.js").read_text(encoding="utf-8")
        expression = re.search(
            r'\$\("log"\)\.textContent \+= (event\.line \+ "[^"]*")', source,
        )
        if expression is None:
            raise AssertionError(
                "affectation de $(\"log\").textContent += event.line + ... introuvable")
        cls.expression = expression.group(1)

    def test_ligne_suivie_d_un_vrai_saut_de_ligne(self):
        script = f"""
const event = {{ line: "X" }};
process.stdout.write(JSON.stringify({self.expression}));
"""
        resultat = subprocess.run(
            [self.node, "-e", script], capture_output=True, text=True, timeout=60,
        )
        if resultat.returncode != 0:
            raise AssertionError(resultat.stderr)
        # "X\n" une fois décodé : un caractère de saut de ligne réel, pas les
        # deux caractères « \ » et « n » que produisait le bug.
        self.assertEqual(json.loads(resultat.stdout), "X\n")


if __name__ == "__main__":
    unittest.main()
