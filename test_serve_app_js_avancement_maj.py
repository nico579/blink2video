"""La barre de mise à jour parle en mégaoctets entiers.

Vu en vrai le 2026-10-17, pendant la première mise à jour par l'installateur
commun : « Téléchargement de la mise à jour (83 Mo) 77.5/82.67831420898438
(93 %) ». La ligne d'avancement comptait des éléments (le clip 5 sur 40, d'où
son « +1 ») et recevait ici des mégaoctets décimaux. Le vrai code de
serve_app.js, exécuté par node avec un faux DOM."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

SOURCE = (Path(__file__).parent / "serve_app.js").read_text(encoding="utf-8")
CODE = "\n".join(re.search(motif, SOURCE, re.DOTALL | re.MULTILINE).group(0) for motif in (
    r"^const I18N = \{.*?^\};",
    r"^function t\([^\n]*$",
    *[rf"^function {nom}\(.*?^\}}" for nom in ("tf", "libellePhase", "montrerTravail")],
))


def phase(travail: dict, langue: str = "fr") -> str:
    node = shutil.which("node")
    if node is None:
        raise unittest.SkipTest("node introuvable")
    script = f"""
const elements = {{}};
const $ = id => elements[id] ||= {{ disabled: false, dataset: {{}}, textContent: '',
  classList: {{ add() {{}}, remove() {{}} }}, removeAttribute() {{}} }};
let _lang = {json.dumps(langue)}, actualisationLocale = false;
let travailVisible = false, travailEnCours = false, miseAJourAttente = null;
const rechargerEnArrierePlan = () => {{}};
{CODE}
montrerTravail({json.dumps(travail)});
console.log(JSON.stringify($("phase").textContent));
"""
    resultat = subprocess.run([node, "-"], input=script, capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
    assert resultat.returncode == 0, resultat.stderr
    return json.loads(resultat.stdout)


MAJ = {"quoi": "Téléchargement de la mise à jour (76 Mo)", "cle": "phase.update_download"}
TOTAL = 82.67831420898438


class AvancementMiseAJour(unittest.TestCase):
    def test_megaoctets_entiers_sans_decimales(self):
        self.assertEqual(phase(dict(MAJ, fait=76.5, total=TOTAL)),
                         "Téléchargement de la mise à jour (83 Mo) 76/83 (93 %)")

    def test_la_fin_dit_le_total(self):
        self.assertEqual(phase(dict(MAJ, fait=TOTAL, total=TOTAL)),
                         "Téléchargement de la mise à jour (83 Mo) 83/83 (100 %)")

    def test_en_anglais(self):
        self.assertEqual(phase(dict(MAJ, fait=10.2, total=TOTAL), "en"),
                         "Downloading the update (83 MB) 10/83 (12 %)")

    def test_un_compte_d_elements_garde_son_rang(self):
        self.assertEqual(phase({"quoi": "Téléchargement", "cle": "", "fait": 4, "total": 40}),
                         "Téléchargement 5/40 (10 %)")


if __name__ == "__main__":
    unittest.main()
