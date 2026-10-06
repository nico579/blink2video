"""Audit du 2026-10-02, B13 : si la requête de mise à jour échoue (serveur
injoignable, réponse illisible), le bouton restait grisé jusqu'au rechargement
de la page. Il doit se rendre, avec la version proposée, tant que le serveur n'a
pas confirmé le lancement. Vrai gestionnaire extrait de serve_app.js, exécuté
par Node avec un DOM minimal."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


class TestsMajRequeteEchouee(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node introuvable")
        source = (Path(__file__).parent / "serve_app.js").read_text(encoding="utf-8")
        motifs = [
            r"^const I18N = \{.*?^\};",
            r"^function t\([^\n]*$",
            r"^const CONTROLES_GELES_PENDANT_MAJ = [^\n]*;$",
            *[rf"^function {nom}\(.*?^\}}" for nom in ("tf", "montrerMaj", "gelerPendantMaj")],
            r'^\$\("update"\)\.onclick = async \(\) => \{.*?^\};',
        ]
        cls.code = "\n".join(re.search(motif, source, re.DOTALL | re.MULTILINE).group(0)
                             for motif in motifs)

    def executer(self, scenario, langue="fr"):
        script = """
const elements = {};
const $ = id => elements[id] ||= {
  disabled: false, dataset: {}, textContent: '', hidden: false, title: '',
  classList: {add() {}, remove() {}}, removeAttribute() {}
};
let _lang = LANGUE;
let miseAJourAttente = null, generationMaj = 0;
const messages = [];
const alert = texte => messages.push(texte);
const setInterval = () => 7, clearInterval = () => {}, setTimeout = () => 8;
const SCENARIO = %s;
const fetch = async () => {
  if (SCENARIO === 'rejet') throw new TypeError('Failed to fetch');
  return {};
};
const lireJSON = async () => {
  if (SCENARIO === 'illisible') throw new SyntaxError('Unexpected token');
  if (SCENARIO === 'refus') return {error: 'Aucune version plus récente.'};
  return {version: '0.99.0'};
};
""" % json.dumps(scenario)
        script = script.replace("LANGUE", json.dumps(langue)) + self.code + """
(async () => {
  montrerMaj({version: '0.99.0'});
  const libelleInitial = $('update').textContent;
  await $('update').onclick();
  console.log(JSON.stringify({
    desactive: $('update').disabled, encours: $('update').dataset.encours ?? null,
    libelle: $('update').textContent, libelleInitial, messages,
    attente: miseAJourAttente,
  }));
})();
"""
        resultat = subprocess.run([self.node, "-"], input=script, capture_output=True,
                                  text=True, encoding="utf-8", timeout=30)
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        return json.loads(resultat.stdout)

    def test_requete_rejetee_rend_le_bouton_et_dit_pourquoi(self):
        etat = self.executer("rejet")
        self.assertFalse(etat["desactive"])
        self.assertIsNone(etat["encours"])
        self.assertEqual(etat["libelle"], etat["libelleInitial"])
        self.assertEqual(len(etat["messages"]), 1)
        self.assertIn("Failed to fetch", etat["messages"][0])
        self.assertIn("n'a pas pu être lancée", etat["messages"][0])
        self.assertIsNone(etat["attente"])

    def test_reponse_illisible_rend_aussi_le_bouton(self):
        etat = self.executer("illisible")
        self.assertFalse(etat["desactive"])
        self.assertIsNone(etat["encours"])
        self.assertEqual(etat["libelle"], etat["libelleInitial"])

    def test_message_en_anglais(self):
        etat = self.executer("rejet", langue="en")
        self.assertIn("could not be started", etat["messages"][0])

    def test_temoin_refus_du_serveur_inchange(self):
        etat = self.executer("refus")
        self.assertFalse(etat["desactive"])
        self.assertEqual(etat["messages"], ["Aucune version plus récente."])

    def test_temoin_lancement_confirme_garde_le_bouton_gele(self):
        etat = self.executer("succes")
        self.assertTrue(etat["desactive"])
        self.assertEqual(etat["encours"], "1")
        self.assertEqual(etat["messages"], [])
        self.assertEqual(etat["attente"], 7)

    def test_les_deux_langues_ont_le_libelle(self):
        source = (Path(__file__).parent / "serve_app.js").read_text(encoding="utf-8")
        self.assertEqual(source.count('"update.failed": '), 2)


if __name__ == "__main__":
    unittest.main()
