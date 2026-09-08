"""Caractérise le formulaire réel dans Node, sans navigateur ni appel réseau."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


REGLAGES = {
    "usb_minutes": 17, "cloud_minutes": 3, "port": 8127,
    "storage_dir": "C:/Vidéos & archives/Blink", "timestamp": True,
    "timezone": "Europe/Paris", "live_protocol": "webrtc",
    "merge_jour": True, "merge_semaine": True, "merge_mois": False,
    "download_auto": True, "initial_setup": False,
}


class TestsFormulaireReglages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node introuvable")
        source = (Path(__file__).parent / "serve_app.js").read_text(encoding="utf-8")
        debut = source.index("let portActuel = null;")
        fin = source.index('$("filtreButton").onclick', debut)
        fragments = [source[debut:fin]]
        for nom in ("lireJSON", "appliquerDependanceMergeJour", "appliquerDependanceDownloadAuto"):
            correspondance = re.search(
                rf"^(?:async )?function {nom}\(.*?^\}}", source, re.DOTALL | re.MULTILINE,
            )
            if correspondance is None:
                raise AssertionError(f"fonction {nom}() introuvable")
            fragments.append(correspondance.group(0))
        # Conserve aussi les branchements réels des cases, pas des appels de test
        # aux fonctions internes dont le découpage peut évoluer.
        for identifiant in ("mergeJour", "downloadAuto"):
            correspondance = re.search(
                rf'^\$\("{identifiant}"\)\.onchange = .*?;$', source, re.MULTILINE,
            )
            if correspondance is None:
                raise AssertionError(f"onchange de {identifiant} introuvable")
            fragments.append(correspondance.group(0))
        cls.javascript = "\n".join(fragments)

    def _executer(self, ouvertures=None, actions=None):
        script = r"""
const params = JSON.parse(process.argv[1]);
const evenements = [], captures = [];
let rechargements = 0, reponseCourante, libererGet;
const attenteListes = new Promise(() => {});
const ids = [
  'usbMinutes', 'cloudMinutes', 'port', 'storageDir', 'timestamp', 'timezone',
  'liveProtocol', 'mergeJour', 'mergeSemaine', 'mergeMois', 'downloadAuto',
  'initialSetupHint', 'reglagesClose', 'stopButton', 'reglages', 'reglagesButton',
];
class Element {
  constructor(id) {
    this.id = id;
    this.value = `avant:${id}`;
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.dataset = {};
    this.ecouteurs = {};
  }
  set value(valeur) { this.valeur = String(valeur); }
  get value() { return this.valeur; }
  addEventListener(type, fonction) { this.ecouteurs[type] = fonction; }
  showModal() {
    evenements.push('showModal');
    captures.push(capturer());
  }
  close() { evenements.push('close'); }
}
const elements = Object.fromEntries(ids.map((id) => [id, new Element(id)]));
globalThis.$ = (id) => {
  if (!elements[id]) throw new Error(`Élément inattendu : ${id}`);
  return elements[id];
};
globalThis.location = {reload: () => { rechargements += 1; }};
globalThis.chargerSourdine = () => {
  evenements.push('chargerSourdine');
  return attenteListes;
};
globalThis.chargerSuppressionAuto = () => {
  evenements.push('chargerSuppressionAuto');
  return attenteListes;
};
globalThis.fetch = async (url, options) => {
  if (url !== '/api/reglages' || options !== undefined) {
    throw new Error(`Requête inattendue : ${url}`);
  }
  evenements.push('fetch');
  await new Promise((resolve) => { libererGet = resolve; });
  if (reponseCourante.erreur === 'reseau') throw new TypeError('GET indisponible');
  return {json: async () => {
    evenements.push('json');
    if (reponseCourante.erreur === 'json') throw new SyntaxError('JSON invalide');
    return reponseCourante.reglages;
  }};
};
""" + self.javascript + r"""
function capturer() {
  return {
    portActuel,
    champs: Object.fromEntries(ids.map((id) => [id, {
      value: $(id).value, checked: $(id).checked, disabled: $(id).disabled,
      hidden: $(id).hidden, dataset: {...$(id).dataset},
    }])),
  };
}
(async () => {
  const avantGet = [], apresActions = [];
  for (const ouverture of params.ouvertures) {
    reponseCourante = ouverture;
    const attente = ouverture.bouton ? $('reglagesButton').onclick() :
      ouvrirReglages(ouverture.initiale);
    avantGet.push({etat: capturer(), evenements: [...evenements]});
    libererGet();
    await attente;
  }
  for (const action of params.actions) {
    if (action.type === 'case') {
      $(action.id).checked = action.checked;
      $(action.id).onchange();
      apresActions.push(capturer());
    } else if (action.type === 'cancel') {
      let empeche = false;
      $('reglages').ecouteurs.cancel({preventDefault: () => { empeche = true; }});
      apresActions.push({empeche});
    } else if (action.type === 'fermer') {
      $('reglagesClose').onclick();
    } else {
      throw new Error(`Action inconnue : ${action.type}`);
    }
  }
  process.stdout.write(JSON.stringify({
    avantGet, captures, apresActions, evenements, rechargements, final: capturer(),
  }));
})().catch((erreur) => { console.error(erreur); process.exitCode = 1; });
"""
        resultat = subprocess.run(
            [self.node, "-e", script, json.dumps({
                "ouvertures": ouvertures if ouvertures is not None else [{"reglages": REGLAGES}],
                "actions": actions or [],
            })],
            capture_output=True, text=True, encoding="utf-8", check=True, timeout=30,
        )
        self.assertTrue(resultat.stdout, "ouvrirReglages ne doit pas attendre les listes")
        return json.loads(resultat.stdout)

    def test_tous_les_champs_et_protocoles_sont_affiches(self):
        valeurs = {
            "usbMinutes": "usb_minutes", "cloudMinutes": "cloud_minutes", "port": "port",
            "storageDir": "storage_dir", "timezone": "timezone", "liveProtocol": "live_protocol",
        }
        cases = {
            "timestamp": "timestamp", "mergeJour": "merge_jour", "mergeSemaine": "merge_semaine",
            "mergeMois": "merge_mois", "downloadAuto": "download_auto",
        }
        for protocole in ("webrtc", "mse"):
            with self.subTest(protocole=protocole):
                reglages = dict(REGLAGES, live_protocol=protocole)
                resultat = self._executer([{"reglages": reglages}])
                etat = resultat["captures"][0]
                for identifiant, cle in valeurs.items():
                    self.assertEqual(etat["champs"][identifiant]["value"], str(reglages[cle]))
                for identifiant, cle in cases.items():
                    self.assertEqual(etat["champs"][identifiant]["checked"], reglages[cle])
                self.assertEqual(etat["portActuel"], 8127)

    def test_ouverture_attend_reglages_puis_lance_listes_avant_dialogue(self):
        resultat = self._executer()
        self.assertEqual(resultat["avantGet"][0]["evenements"], ["fetch"])
        self.assertIsNone(resultat["avantGet"][0]["etat"]["portActuel"])
        self.assertEqual(resultat["avantGet"][0]["etat"]["champs"]["usbMinutes"]["value"],
                         "avant:usbMinutes")
        self.assertEqual(resultat["evenements"], [
            "fetch", "json", "chargerSourdine", "chargerSuppressionAuto", "showModal",
        ])
        self.assertEqual(len(resultat["captures"]), 1)

    def test_configuration_initiale_appel_ou_serveur_bloque_echap(self):
        for initiale in (False, True):
            for serveur in (False, True):
                with self.subTest(initiale=initiale, serveur=serveur):
                    resultat = self._executer([{
                        "initiale": initiale, "reglages": dict(REGLAGES, initial_setup=serveur),
                    }], [{"type": "cancel"}])
                    actif = initiale or serveur
                    champs = resultat["final"]["champs"]
                    self.assertEqual(champs["initialSetupHint"]["hidden"], not actif)
                    self.assertEqual(champs["reglagesClose"]["hidden"], actif)
                    self.assertEqual(champs["stopButton"]["hidden"], actif)
                    self.assertEqual(champs["reglages"]["dataset"], {"initialSetup": "1" if actif else "0"})
                    self.assertEqual(resultat["apresActions"], [{"empeche": actif}])

    def test_boutons_ouvrent_mode_ordinaire_et_ferment_dialogue(self):
        resultat = self._executer([
            {"initiale": True, "reglages": REGLAGES},
            {"bouton": True, "reglages": dict(REGLAGES, port=9000)},
        ], [{"type": "cancel"}, {"type": "fermer"}])
        self.assertEqual(resultat["captures"][0]["champs"]["reglages"]["dataset"]["initialSetup"], "1")
        self.assertEqual(resultat["captures"][1]["champs"]["reglages"]["dataset"]["initialSetup"], "0")
        self.assertEqual(resultat["final"]["portActuel"], 9000)
        self.assertEqual(resultat["apresActions"], [{"empeche": False}])
        self.assertEqual(resultat["evenements"][-1], "close")

    def test_sans_fusion_journaliere_semaine_et_mois_sont_decoches(self):
        resultat = self._executer([{
            "reglages": dict(REGLAGES, merge_jour=False, merge_semaine=True, merge_mois=True),
        }], [{"type": "case", "id": "mergeJour", "checked": True}])
        for identifiant in ("mergeSemaine", "mergeMois"):
            self.assertFalse(resultat["captures"][0]["champs"][identifiant]["checked"])
            self.assertTrue(resultat["captures"][0]["champs"][identifiant]["disabled"])
            self.assertFalse(resultat["final"]["champs"][identifiant]["checked"])
            self.assertFalse(resultat["final"]["champs"][identifiant]["disabled"])

    def test_decocher_jour_met_a_jour_dependances_immediatement(self):
        resultat = self._executer(actions=[
            {"type": "case", "id": "mergeJour", "checked": False},
            {"type": "case", "id": "mergeJour", "checked": True},
        ])
        for indice, disabled in enumerate((True, False)):
            for identifiant in ("mergeSemaine", "mergeMois"):
                champ = resultat["apresActions"][indice]["champs"][identifiant]
                self.assertEqual(champ["disabled"], disabled)
                self.assertFalse(champ["checked"])

    def test_sans_telechargement_cadences_grisees_mais_valeurs_conservees(self):
        resultat = self._executer([{"reglages": dict(REGLAGES, download_auto=False)}], [
            {"type": "case", "id": "downloadAuto", "checked": True},
            {"type": "case", "id": "downloadAuto", "checked": False},
        ])
        etats = [resultat["captures"][0]] + resultat["apresActions"]
        for etat, disabled in zip(etats, (True, False, True)):
            for identifiant, valeur in (("usbMinutes", "17"), ("cloudMinutes", "3")):
                self.assertEqual(etat["champs"][identifiant]["disabled"], disabled)
                self.assertEqual(etat["champs"][identifiant]["value"], valeur)
            self.assertTrue(etat["champs"]["mergeJour"]["checked"])
            self.assertTrue(etat["champs"]["mergeSemaine"]["checked"])

    def test_echec_get_conserve_champs_et_port_precedents(self):
        for erreur in ("reseau", "json"):
            with self.subTest(erreur=erreur):
                resultat = self._executer([
                    {"reglages": REGLAGES}, {"erreur": erreur},
                ])
                self.assertEqual(resultat["captures"][0], resultat["captures"][1])
                self.assertEqual(resultat["rechargements"], int(erreur == "json"))
                self.assertEqual(resultat["evenements"][-3:], [
                    "chargerSourdine", "chargerSuppressionAuto", "showModal",
                ])

    def test_echec_get_initial_conserve_protection_du_premier_demarrage(self):
        resultat = self._executer([{"initiale": True, "erreur": "reseau"}], [{"type": "cancel"}])
        self.assertIsNone(resultat["final"]["portActuel"])
        self.assertEqual(resultat["final"]["champs"]["usbMinutes"]["value"], "avant:usbMinutes")
        self.assertTrue(resultat["final"]["champs"]["reglagesClose"]["hidden"])
        self.assertTrue(resultat["final"]["champs"]["stopButton"]["hidden"])
        self.assertEqual(resultat["apresActions"], [{"empeche": True}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
