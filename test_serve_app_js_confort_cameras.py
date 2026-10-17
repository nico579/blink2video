"""Issue #40 : tri naturel des caméras, taille des vignettes, caméras masquées.
Le vrai code de serve_app.js, exécuté par node avec un faux DOM et un faux
localStorage ; les contrôles de structure portent sur serve.py et le CSS."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

RACINE = Path(__file__).parent
SOURCE = (RACINE / "serve_app.js").read_text(encoding="utf-8")


def extraire(nom: str) -> str:
    trouve = re.search(rf"^(?:async )?function {nom}\(.*?^\}}", SOURCE, re.DOTALL | re.MULTILINE)
    if trouve is None:
        raise AssertionError(f"fonction {nom}() introuvable dans serve_app.js")
    return trouve.group(0)


FONCTIONS = [
    "restaurerCamerasMasquees", "memoriserCamerasMasquees", "estMasquee",
    "restaurerTailleCartes", "appliquerTailleCartes", "comparerNoms",
    "majCaseMasquees", "toutesLesCameras", "camerasConnues", "camerasPourMasquage",
    "visible", "renderLive",
]

# Constantes et état du bloc « préférences d'affichage » : recopiés du source,
# jamais réécrits ici.
CONSTANTES = "\n".join(
    re.search(rf"^{motif}.*?;$", SOURCE, re.MULTILINE).group(0)
    for motif in (r"const CLE_CAMERAS_MASQUEES", r"const CLE_TAILLE_CARTES",
                  r"const TAILLES_CARTES", r"const TAILLE_CARTES_DEFAUT",
                  r"const _collateurs")
)


def executer(scenario: str, stockage: dict | None = None, systeme: dict | None = None) -> dict:
    node = shutil.which("node")
    if node is None:
        raise unittest.SkipTest("node introuvable")
    script = f"""
const magasin = {json.dumps(stockage or {})};
globalThis.localStorage = {{
  getItem: (c) => (c in magasin ? magasin[c] : null),
  setItem: (c, v) => {{ magasin[c] = String(v); }},
}};
const proprietes = {{}};
globalThis.document = {{ documentElement: {{ style: {{ setProperty: (k, v) => {{ proprietes[k] = v; }} }} }} }};
const elements = {{
  hiddenLabel: {{ hidden: true }}, showHidden: {{ checked: false }},
  showOut: {{ checked: true }}, camera: {{ value: "" }}, view: {{ value: "clips" }},
  list: {{ innerHTML: "" }}, count: {{ textContent: "" }},
}};
function $(id) {{ return elements[id]; }}
let _lang = "fr";
let data = {{ clips: [], cameras: [] }}, videos = {{ daily: [] }}, snapshots = [];
let system = {json.dumps(systeme)};
const nomsDirectsActifs = () => [];
const ronde = {{ boite: null }};
const t = (cle) => cle, tf = (cle, p) => cle + JSON.stringify(p), h = (s) => String(s);
const cameraCard = (c) => `<card ${{c.name}}>`;
let rafraichirVignettes = false;
function actualiserVignettes() {{}}
function loadSystem() {{}}
{CONSTANTES}
let camerasMasquees = restaurerCamerasMasquees();
let montrerMasquees = false;
let tailleCartes = restaurerTailleCartes();
{chr(10).join(extraire(nom) for nom in FONCTIONS)}
const sortie = {{}};
{scenario}
console.log(JSON.stringify(sortie));
"""
    resultat = subprocess.run([node, "-"], input=script, capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
    assert resultat.returncode == 0, resultat.stderr
    return json.loads(resultat.stdout)


class TriNaturelDesCameras(unittest.TestCase):
    def test_les_nombres_sont_compares_comme_des_nombres(self):
        r = executer('sortie.tri = ["10-Cave", "2-Escalier", "1-Haustür", "abeille", "Zébra"].sort(comparerNoms);')
        self.assertEqual(r["tri"], ["1-Haustür", "2-Escalier", "10-Cave", "abeille", "Zébra"])

    def test_le_filtre_est_trie_dans_cet_ordre(self):
        r = executer('data.cameras = ["10-Cave", "2-Escalier", "1-Porte"]; sortie.liste = camerasConnues();')
        self.assertEqual(r["liste"], ["1-Porte", "2-Escalier", "10-Cave"])


class CamerasMasquees(unittest.TestCase):
    STOCK = {"blink2video.camerasMasquees": json.dumps(["Grenier", "Cave"])}

    def test_une_camera_masquee_disparait_du_filtre(self):
        r = executer('data.cameras = ["Cave", "Salon", "Grenier"]; sortie.liste = camerasConnues();', self.STOCK)
        self.assertEqual(r["liste"], ["Salon"])

    def test_le_devoilement_temporaire_les_remet_sans_rien_memoriser(self):
        r = executer(
            'data.cameras = ["Cave", "Salon"]; montrerMasquees = true;'
            'sortie.liste = camerasConnues(); sortie.stock = JSON.stringify(magasin);', self.STOCK)
        self.assertEqual(r["liste"], ["Cave", "Salon"])
        self.assertEqual(json.loads(r["stock"]), self.STOCK)

    def test_les_clips_d_une_camera_masquee_ne_sont_pas_visibles(self):
        r = executer(
            'data.clips = [{kind:"clip", camera:"Cave", excluded:false}, {kind:"clip", camera:"Salon", excluded:false}];'
            'sortie.vus = visible().map((c) => c.camera); montrerMasquees = true;'
            'sortie.apres = visible().map((c) => c.camera);', self.STOCK)
        self.assertEqual(r["vus"], ["Salon"])
        self.assertEqual(r["apres"], ["Cave", "Salon"])

    def test_la_liste_des_reglages_garde_les_masquees_et_celles_du_direct(self):
        systeme = {"systems": [{"cameras": [{"name": "Garage"}, {"name": "Salon"}]}]}
        r = executer('data.cameras = ["Salon"]; sortie.liste = camerasPourMasquage();',
                     {"blink2video.camerasMasquees": json.dumps(["Ancienne"])}, systeme)
        self.assertEqual(r["liste"], ["Ancienne", "Garage", "Salon"])

    def test_la_case_de_devoilement_n_existe_que_si_une_camera_est_masquee(self):
        r = executer('majCaseMasquees(); sortie.libre = $("hiddenLabel").hidden;')
        self.assertTrue(r["libre"])
        r = executer('majCaseMasquees(); sortie.libre = $("hiddenLabel").hidden;', self.STOCK)
        self.assertFalse(r["libre"])

    def test_sans_masquee_le_devoilement_retombe(self):
        r = executer('montrerMasquees = true; $("showHidden").checked = true; majCaseMasquees();'
                     'sortie.montrer = montrerMasquees; sortie.coche = $("showHidden").checked;')
        self.assertFalse(r["montrer"])
        self.assertFalse(r["coche"])

    def test_un_stockage_abime_ne_casse_rien(self):
        for valeur in ("pas du json", '{"a": 1}', "null", "[1, 2, null]"):
            with self.subTest(valeur=valeur):
                r = executer('sortie.n = camerasMasquees.size;', {"blink2video.camerasMasquees": valeur})
                self.assertEqual(r["n"], 0)


class VuesDirect(unittest.TestCase):
    SYSTEME = {"error": None, "systems": [
        {"name": "Maison", "key": "m", "armed": True, "module": None, "module_firmware": None,
         "module_serial": None,
         "cameras": [{"name": "10-Cave", "armed": True}, {"name": "2-Salon", "armed": False},
                     {"name": "Grenier", "armed": True}]},
        {"name": "Loft", "key": "l", "armed": False, "module": None, "module_firmware": None,
         "module_serial": None, "cameras": [{"name": "Grenier", "armed": True}]},
    ]}

    def test_le_direct_masque_et_trie_les_cameras(self):
        r = executer('renderLive(); sortie.html = $("list").innerHTML; sortie.compte = $("count").textContent;',
                     {"blink2video.camerasMasquees": json.dumps(["Grenier"])}, self.SYSTEME)
        html = r["html"]
        self.assertLess(html.index("<card 2-Salon>"), html.index("<card 10-Cave>"))
        self.assertNotIn("<card Grenier>", html)
        # Un système dont toutes les caméras sont masquées garde son titre et son
        # armement, et dit pourquoi sa grille est vide.
        self.assertIn("Loft", html)
        self.assertIn("live.toutesMasquees", html)
        self.assertIn('"n":2', r["compte"])
        self.assertIn('"m":1', r["compte"])

    def test_rien_n_est_masque_par_defaut(self):
        r = executer('renderLive(); sortie.html = $("list").innerHTML;', None, self.SYSTEME)
        self.assertEqual(r["html"].count("<card Grenier>"), 2)
        self.assertNotIn("live.toutesMasquees", r["html"])


class TailleDesVignettes(unittest.TestCase):
    def test_la_taille_par_defaut_est_celle_d_avant(self):
        r = executer("appliquerTailleCartes(); sortie.p = proprietes;")
        self.assertEqual(r["p"], {"--carte-min": "320px"})

    def test_les_quatre_tailles_et_la_valeur_inconnue(self):
        attendu = {"petites": "220px", "moyennes": "320px", "grandes": "480px", "tres_grandes": "720px"}
        for nom, px in attendu.items():
            with self.subTest(nom=nom):
                r = executer("appliquerTailleCartes(); sortie.p = proprietes;",
                             {"blink2video.tailleCartes": nom})
                self.assertEqual(r["p"]["--carte-min"], px)
        for abime in ("enorme", "constructor", "__proto__", ""):
            with self.subTest(abime=abime):
                r = executer("appliquerTailleCartes(); sortie.p = proprietes;",
                             {"blink2video.tailleCartes": abime})
                self.assertEqual(r["p"]["--carte-min"], "320px")


class StructureDeLaPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (RACINE / "serve.py").read_text(encoding="utf-8")
        cls.css = (RACINE / "serve_style.css").read_text(encoding="utf-8")

    def test_les_commandes_existent_dans_la_page(self):
        for identifiant in ("showHidden", "hiddenLabel", "tailleCartes", "camerasMasqueesListe"):
            self.assertEqual(self.html.count(f'id="{identifiant}"'), 1, identifiant)

    def test_la_case_de_devoilement_est_cachee_au_depart(self):
        self.assertRegex(self.html, r'<label id="hiddenLabel" hidden>')

    def test_le_css_lit_la_taille_choisie_et_garde_le_rapport_des_grilles_larges(self):
        self.assertIn("minmax(var(--carte-min, 320px), 1fr)", self.css)
        self.assertIn("calc(var(--carte-min, 320px) * 1.4375)", self.css)

    def test_chaque_nouveau_texte_existe_en_francais_et_en_anglais(self):
        cles = re.findall(r'data-i18n="((?:filtre\.showHidden|reglages\.tailleCartes[\w.]*|reglages\.masquees[\w.]*))"', self.html)
        cles += ["masquees.none", "live.toutesMasquees"]
        self.assertTrue(len(cles) >= 8, cles)
        for cle in cles:
            self.assertEqual(len(re.findall(rf'"{re.escape(cle)}":', SOURCE)), 2,
                             f"{cle} : attendu une fois en français et une fois en anglais")


if __name__ == "__main__":
    unittest.main()
