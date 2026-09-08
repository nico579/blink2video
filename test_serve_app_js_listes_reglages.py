"""Exécute les listes de réglages réelles dans Node, sans navigateur ni API.

Le faux DOM interdit toute insertion HTML et les réponses différées vérifient
aussi les états transitoires : chargement et case bloquée pendant un POST.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


NOM_SENSIBLE = '<img src=x onerror="alert(1)"> Jardin & Terrasse\'s'


class TestsListesReglagesCameras(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node introuvable")
        source = (Path(__file__).parent / "serve_app.js").read_text(encoding="utf-8")
        fonctions = []
        for nom in (
            "lireJSON", "chargerListeReglageCameras",
            "chargerSourdine", "chargerSuppressionAuto",
        ):
            correspondance = re.search(
                rf"^async function {nom}\(.*?^\}}", source, re.DOTALL | re.MULTILINE,
            )
            if correspondance is None:
                if nom == "chargerListeReglageCameras":
                    continue  # Permet aussi de caractériser la version non factorisée.
                raise AssertionError(f"fonction {nom}() introuvable")
            fonctions.append(correspondance.group(0))
        cls.fonctions = "\n".join(fonctions)

    def _executer(self, type_liste, mode, initial=False):
        script = r"""
const params = JSON.parse(process.argv[1]);
const requetes = [], alertes = [], traductions = [];
let rechargements = 0, langue = 'fr';
globalThis.location = {reload: () => { rechargements += 1; }};
globalThis.alert = (message) => { alertes.push(message); };
globalThis.t = (cle) => { traductions.push(cle); return `${langue}:${cle}`; };
class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.disabled = false;
    this.checked = false;
  }
  set textContent(value) { this.children = [String(value)]; }
  get textContent() {
    return this.children.map((e) => typeof e === 'string' ? e : e.textContent).join('');
  }
  set innerHTML(value) {
    if (value !== '') throw new Error('Insertion HTML interdite');
    this.children = [];
  }
  replaceChildren(...children) { this.children = [...children]; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { this.children.push(...children); }
}
const conteneurs = {
  sourdineListe: new Element('div'), suppressionAutoListe: new Element('div'),
};
globalThis.document = {createElement: (tag) => new Element(tag)};
globalThis.$ = (id) => conteneurs[id];
const sourdine = params.type === 'sourdine';
const prefixe = sourdine ? 'sourdine' : 'suppressionAuto';
const conteneur = conteneurs[`${prefixe}Liste`];
conteneur.appendChild(new Element('ancien'));
let libererGet, libererPost;
const attenteGet = new Promise((resolve) => { libererGet = resolve; });
const attentePost = new Promise((resolve) => { libererPost = resolve; });
const noms = ['Salon', params.nom];
const etat = {
  cameras: params.mode.startsWith('vide') ? [] :
    (sourdine ? noms : noms.map((name, index) => ({name, keys: [`cle-${index}`]}))),
  [sourdine ? 'ignored' : 'actives']: params.initial ? [params.nom] : [],
};
globalThis.fetch = async (url, options) => {
  requetes.push({url, options: options || null});
  if (!options) {
    await attenteGet;
    if (params.mode === 'get_reseau') throw new TypeError('GET réseau');
    return {json: async () => {
      if (params.mode === 'get_json') throw new SyntaxError('GET JSON');
      return etat;
    }};
  }
  await attentePost;
  if (params.mode === 'post_reseau') throw new TypeError('POST réseau');
  return {json: async () => {
    if (params.mode === 'post_json') throw new SyntaxError('POST JSON');
    return params.mode === 'post_refus' ? {error: 'Refus serveur'} : {ok: true};
  }};
};
""" + self.fonctions + r"""
(async () => {
  const charger = sourdine ? chargerSourdine : chargerSuppressionAuto;
  const chargement = charger();
  const pendantChargement = conteneur.textContent;
  if (params.mode === 'vide_langue') langue = 'en';
  libererGet();
  await chargement;
  const lignes = conteneur.children.filter((e) => typeof e !== 'string').map((label) => ({
    tag: label.tag, texte: label.textContent,
    enfants: label.children.map((e) => typeof e === 'string' ? 'texte' : e.tag),
    type: label.children[0].type, checked: label.children[0].checked,
  }));
  let pendantPost = null, apresPost = null;
  if (params.mode.startsWith('post_')) {
    const case_ = conteneur.children[1].children[0];
    case_.checked = !params.initial;
    const sauvegarde = case_.onchange();
    pendantPost = {disabled: case_.disabled, checked: case_.checked};
    libererPost();
    await sauvegarde;
    apresPost = {disabled: case_.disabled, checked: case_.checked};
  }
  process.stdout.write(JSON.stringify({
    requetes, alertes, traductions, rechargements, pendantChargement,
    texte: conteneur.textContent, lignes, pendantPost, apresPost,
  }));
})().catch((erreur) => { console.error(erreur); process.exitCode = 1; });
"""
        resultat = subprocess.run(
            [self.node, "-e", script, json.dumps({
                "type": type_liste, "mode": mode, "initial": initial, "nom": NOM_SENSIBLE,
            })],
            capture_output=True, text=True, encoding="utf-8", check=True, timeout=30,
        )
        return json.loads(resultat.stdout)

    def _verifier_get(self, resultat, type_liste):
        url = "/api/sourdine" if type_liste == "sourdine" else "/api/suppression-auto"
        self.assertEqual(resultat["requetes"][0], {"url": url, "options": None})
        self.assertEqual(resultat["pendantChargement"], f"fr:{type_liste}.loading")

    def test_affichage_et_selection_sans_interpretation_html(self):
        for type_liste in ("sourdine", "suppressionAuto"):
            with self.subTest(type_liste=type_liste):
                resultat = self._executer(type_liste, "affichage", initial=True)
                self._verifier_get(resultat, type_liste)
                self.assertEqual(resultat["lignes"], [
                    {"tag": "label", "texte": " Salon", "enfants": ["input", "texte"],
                     "type": "checkbox", "checked": False},
                    {"tag": "label", "texte": f" {NOM_SENSIBLE}",
                     "enfants": ["input", "texte"], "type": "checkbox", "checked": True},
                ])
                self.assertEqual(resultat["alertes"], [])
                self.assertEqual(len(resultat["requetes"]), 1)

    def test_liste_vide(self):
        for type_liste in ("sourdine", "suppressionAuto"):
            with self.subTest(type_liste=type_liste):
                resultat = self._executer(type_liste, "vide")
                self._verifier_get(resultat, type_liste)
                self.assertEqual(resultat["texte"], f"fr:{type_liste}.none")
                self.assertEqual(resultat["lignes"], [])

    def test_traduction_reste_dynamique(self):
        for type_liste in ("sourdine", "suppressionAuto"):
            with self.subTest(type_liste=type_liste):
                resultat = self._executer(type_liste, "vide_langue")
                self._verifier_get(resultat, type_liste)
                self.assertEqual(resultat["texte"], f"en:{type_liste}.none")

    def test_erreurs_de_chargement(self):
        for type_liste in ("sourdine", "suppressionAuto"):
            for mode in ("get_reseau", "get_json"):
                with self.subTest(type_liste=type_liste, mode=mode):
                    resultat = self._executer(type_liste, mode)
                    self._verifier_get(resultat, type_liste)
                    self.assertEqual(resultat["texte"], f"fr:{type_liste}.unavailable")
                    self.assertEqual(resultat["lignes"], [])
                    self.assertEqual(resultat["rechargements"], int(mode == "get_json"))
                    self.assertEqual(resultat["alertes"], [])

    def _verifier_post(self, type_liste, mode, initial):
        resultat = self._executer(type_liste, mode, initial)
        self._verifier_get(resultat, type_liste)
        self.assertEqual(len(resultat["requetes"]), 2)
        post = resultat["requetes"][1]
        self.assertEqual(post["url"], resultat["requetes"][0]["url"])
        self.assertEqual(post["options"]["method"], "POST")
        self.assertEqual(post["options"]["headers"], {"Content-Type": "application/json"})
        champ = "ignored" if type_liste == "sourdine" else "actif"
        self.assertEqual(json.loads(post["options"]["body"]), {
            "camera": NOM_SENSIBLE, champ: not initial,
        })
        self.assertEqual(resultat["pendantPost"], {"disabled": True, "checked": not initial})
        self.assertEqual(resultat["apresPost"], {
            "disabled": False, "checked": not initial if mode == "post_ok" else initial,
        })
        self.assertEqual(resultat["rechargements"], int(mode == "post_json"))
        return resultat

    def test_enregistrement_activation_et_desactivation(self):
        for type_liste in ("sourdine", "suppressionAuto"):
            for initial in (False, True):
                with self.subTest(type_liste=type_liste, initial=initial):
                    resultat = self._verifier_post(type_liste, "post_ok", initial)
                    self.assertEqual(resultat["alertes"], [])

    def test_refus_serveur_retablit_la_case(self):
        for type_liste in ("sourdine", "suppressionAuto"):
            for initial in (False, True):
                with self.subTest(type_liste=type_liste, initial=initial):
                    resultat = self._verifier_post(type_liste, "post_refus", initial)
                    self.assertEqual(resultat["alertes"], ["Refus serveur"])

    def test_erreurs_reseau_et_json_retablissent_la_case(self):
        for type_liste in ("sourdine", "suppressionAuto"):
            for mode, message in (("post_reseau", "TypeError: POST réseau"),
                                  ("post_json", "SyntaxError: POST JSON")):
                for initial in (False, True):
                    with self.subTest(type_liste=type_liste, mode=mode, initial=initial):
                        resultat = self._verifier_post(type_liste, mode, initial)
                        self.assertEqual(resultat["alertes"], [message])


if __name__ == "__main__":
    unittest.main(verbosity=2)
