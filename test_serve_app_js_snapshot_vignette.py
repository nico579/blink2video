"""La prise de photo rend son résultat visible sur la vue Direct."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


class TestsSnapshotVignette(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node introuvable")
        source = Path(__file__).with_name("serve_app.js").read_text(encoding="utf-8")
        noms = ("urlVignette", "prendreSnapshot", "actualiserVignettes")
        blocs = []
        for nom in noms:
            fonction = re.search(
                rf"^\s*(?:async )?function {nom}\(.*?^\}}",
                source, re.DOTALL | re.MULTILINE,
            )
            if fonction is None:
                raise AssertionError(f"{nom} introuvable")
            blocs.append(fonction.group(0))
        cls.bloc = "\n".join(blocs)

    def _executer(self, erreur: str = "") -> dict:
        script = f"""
{self.bloc}
const still = {{ src: "ancienne image" }};
const bouton = {{ textContent: "Snapshot", disabled: false, isConnected: true }};
const alertes = [];
let attendre;
const document = {{ querySelector: () => still }};
const cssId = (name) => name.replace(/[^\\w-]/g, "_");
const avecJeton = (url) => `${{url}}&token=test`;
const t = (key) => ({{
  "camera.snapshotting": "Snapshot…", "camera.snapshot.saved": "Snapshot saved ✓",
}})[key];
const $ = () => ({{ value: "live" }});
const fetch = async () => ({{ result: {{ error: {json.dumps(erreur)} }} }});
const lireJSON = async (answer) => answer.result;
const alert = (message) => alertes.push(message);
const setTimeout = (callback) => {{ attendre = callback; }};
(async () => {{
  await prendreSnapshot("Test Camera", bouton);
  const apres = {{ src: still.src, text: bouton.textContent, disabled: bouton.disabled,
                   alertes }};
  if (attendre) attendre();
  process.stdout.write(JSON.stringify({{ apres, final: bouton.textContent }}));
}})();
"""
        resultat = subprocess.run(
            [self.node, "-e", script], capture_output=True, text=True, timeout=10,
        )
        if resultat.returncode != 0:
            raise AssertionError(resultat.stderr)
        return json.loads(resultat.stdout)

    def test_photo_recharge_la_vignette_et_confirme_enregistrement(self):
        etat = self._executer()
        self.assertIn("/camthumb/Test%20Camera?", etat["apres"]["src"])
        self.assertEqual(etat["apres"]["text"], "Snapshot saved ✓")
        self.assertFalse(etat["apres"]["disabled"])
        self.assertEqual(etat["final"], "Snapshot")

    def test_erreur_n_annonce_pas_une_photo_enregistree(self):
        etat = self._executer("Camera busy")
        self.assertEqual(etat["apres"]["src"], "ancienne image")
        self.assertEqual(etat["apres"]["alertes"], ["Camera busy"])
        self.assertEqual(etat["final"], "Snapshot")

    def test_rafraichissement_garde_ancienne_image_jusqu_a_reponse(self):
        script = f"""
{self.bloc}
const still = {{ src: "ancienne image", isConnected: true }};
const cadre = {{ querySelector: (selector) => selector === ".still" ? still
  : {{ dataset: {{ name: "Test Camera" }} }} }};
const $ = () => ({{ querySelectorAll: () => [cadre] }});
const avecJeton = (url) => `${{url}}&token=test`;
let terminer;
const fetch = () => new Promise((resolve) => {{ terminer = resolve; }});
(async () => {{
  actualiserVignettes();
  const avant = still.src;
  terminer({{ ok: true }});
  await new Promise((resolve) => setImmediate(resolve));
  process.stdout.write(JSON.stringify({{ avant, apres: still.src }}));
}})();
"""
        resultat = subprocess.run(
            [self.node, "-e", script], capture_output=True, text=True, timeout=10,
        )
        if resultat.returncode != 0:
            raise AssertionError(resultat.stderr)
        etat = json.loads(resultat.stdout)
        self.assertEqual(etat["avant"], "ancienne image")
        self.assertIn("/camthumb/Test%20Camera?", etat["apres"])


if __name__ == "__main__":
    unittest.main()
