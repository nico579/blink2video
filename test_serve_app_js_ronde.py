"""Issue #40, demande de MCas86 : la ronde des directs, une caméra après
l'autre en plein écran. Le vrai code de serve_app.js, exécuté par node avec
un faux DOM qui sait déplacer un nœud, une fausse horloge et de faux
watchLive()/stopWatch() ; les contrôles de structure portent sur serve.py et
le CSS."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

RACINE = Path(__file__).parent
SOURCE = (RACINE / "serve_app.js").read_text(encoding="utf-8")
PAGE = (RACINE / "serve.py").read_text(encoding="utf-8")
CSS = (RACINE / "serve_style.css").read_text(encoding="utf-8")


def extraire(motif: str) -> str:
    trouve = re.search(motif, SOURCE, re.DOTALL | re.MULTILINE)
    if trouve is None:
        raise AssertionError(f"introuvable dans serve_app.js : {motif}")
    return trouve.group(0)


FONCTIONS = [
    "normaliserReglagesRonde", "lireReglagesRonde", "enregistrerReglagesRonde",
    "dansPlageRonde", "camerasGrille", "camerasRonde", "afficherMessageRonde",
    "armerTourRonde", "imageEnCoursRonde", "rendreCaseRonde", "tourDeRonde",
    "surImageRonde", "demarrerRonde", "arreterRonde", "surPleinEcranRonde",
]

CODE = "\n".join(
    [extraire(r"^const cssId = .*?;$"), extraire(r"^const CLE_RONDE = .*?;$")]
    + re.findall(r"^const RONDE_[A-Z_]+ = .*?;$", SOURCE, re.MULTILINE)
    + [extraire(r"^const ronde = \{.*?^\};"), extraire(r"^let reglagesRonde = .*?;$")]
    + [extraire(rf"^function {nom}\(.*?^\}}") for nom in FONCTIONS]
)

# Faux DOM : juste ce que la ronde touche. Une case de direct par caméra, dans
# sa carte de la grille ; #ronde, sa scène, son titre et son message.
MONTAGE = r"""
class Noeud {
  constructor(nom) { this.nom = nom; this.parent = null; this.enfants = []; this.hidden = false;
                     this.textContent = ""; this.video = null; }
  get isConnected() { let n = this; while (n.parent) n = n.parent; return n === racine; }
  appendChild(e) { if (e.parent) e.parent.retirer(e); e.parent = this; this.enfants.push(e); return e; }
  retirer(e) { this.enfants.splice(this.enfants.indexOf(e), 1); e.parent = null; }
  replaceWith(e) {
    const p = this.parent;
    if (e.parent) e.parent.retirer(e);
    p.enfants[p.enfants.indexOf(this)] = e;
    e.parent = p;
    this.parent = null;
  }
  remove() { if (this.parent) this.parent.retirer(this); }
  contains(e) { for (let n = e; n; n = n.parent) if (n === this) return true; return false; }
  querySelector(selecteur) { return selecteur === "video" ? this.video : null; }
}
const racine = new Noeud("document");
const liste = racine.appendChild(new Noeud("list"));
const elements = { view: { value: "live" } };
for (const nom of ["ronde", "rondeTitre", "rondeMessage", "rondeScene"]) elements[nom] = new Noeud(nom);
racine.appendChild(elements.ronde);
elements.ronde.hidden = true;
for (const nom of ["rondeTitre", "rondeMessage", "rondeScene"]) elements.ronde.appendChild(elements[nom]);
const cartes = {};
for (const cle of ["cam-a", "cam-b", "cam-c", "cam-d"]) {
  cartes[cle] = liste.appendChild(new Noeud("carte-" + cle));
  elements["live-" + cle] = cartes[cle].appendChild(new Noeud("live-" + cle));
}
// Comme getElementById : un nœud détaché du document est introuvable.
function $(id) {
  const e = elements[id];
  return e && (!(e instanceof Noeud) || e.isConnected) ? e : null;
}
const journal = [];
globalThis.document = {
  fullscreenElement: null,
  createComment: () => new Noeud("ancre"),
  exitFullscreen() { journal.push("exitFullscreen"); this.fullscreenElement = null; return Promise.resolve(); },
};
elements.ronde.requestFullscreen = () => { document.fullscreenElement = elements.ronde; return Promise.resolve(); };

const magasin = STOCKAGE;
globalThis.localStorage = {
  getItem: (c) => (c in magasin ? magasin[c] : null),
  setItem: (c, v) => { magasin[c] = String(v); },
};
let minuteries = [];
let numero = 0;
globalThis.setTimeout = (f, d) => { numero += 1; minuteries.push({ id: numero, f, d }); return numero; };
globalThis.clearTimeout = (id) => { minuteries = minuteries.filter((m) => m.id !== id); };
function echeance() { return minuteries.length ? minuteries[0].d : null; }
function declencher() { const m = minuteries.shift(); m.f(); }
const vider = () => new Promise((fin) => setImmediate(fin));

let maintenant = "2026-10-17T12:00:00";
const VraieDate = Date;
globalThis.Date = class extends VraieDate {
  constructor(...a) { super(...(a.length ? a : [maintenant])); }
};

const promesses = {};
function watchLive(cle) {
  journal.push("watch:" + cle);
  return new Promise((fin) => { promesses[cle] = fin; });
}
function stopWatch(cle) { journal.push("stop:" + cle); }
function renderLive() { journal.push("renderLive"); }
const masquees = new Set(MASQUEES);
const estMasquee = (nom) => masquees.has(nom);
const comparerNoms = (a, b) => a.localeCompare(b);
const t = (cle) => cle;
const tf = (cle, p) => cle + JSON.stringify(p);
let system = SYSTEME;
"""

SYSTEME = {"systems": [
    {"name": "Dehors", "cameras": [
        {"name": "B", "key": "cam-b"}, {"name": "A", "key": "cam-a"},
        {"name": "C", "key": "cam-c", "offline": True}]},
    {"name": "Dedans", "cameras": [{"name": "D", "key": "cam-d"}]},
]}


def executer(scenario: str, stockage: dict | None = None, systeme: dict | None = None,
             masquees: tuple = ()) -> dict:
    node = shutil.which("node")
    if node is None:
        raise unittest.SkipTest("node introuvable")
    montage = (MONTAGE.replace("STOCKAGE", json.dumps(stockage or {}))
               .replace("SYSTEME", json.dumps(SYSTEME if systeme is None else systeme))
               .replace("MASQUEES", json.dumps(list(masquees))))
    script = (montage + CODE + "\nconst sortie = {};\n(async () => {\n" + scenario
              + "\nconsole.log(JSON.stringify(sortie));\n})();\n")
    resultat = subprocess.run([node, "-"], input=script, capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
    assert resultat.returncode == 0, resultat.stderr
    return json.loads(resultat.stdout)


class Reglages(unittest.TestCase):
    def test_par_defaut_puis_bornes(self):
        sortie = executer("""
sortie.defaut = normaliserReglagesRonde(null);
sortie.bornes = [normaliserReglagesRonde({secondes: 3}).secondes,
                 normaliserReglagesRonde({secondes: "9999"}).secondes,
                 normaliserReglagesRonde({secondes: "abc"}).secondes,
                 normaliserReglagesRonde({secondes: ""}).secondes,
                 normaliserReglagesRonde({secondes: "45"}).secondes];
sortie.heures = normaliserReglagesRonde({debut: "25:00", fin: "07:30"});
sortie.exclues = normaliserReglagesRonde({exclues: ["cam-a", 3, null]}).exclues;
""")
        self.assertEqual(sortie["defaut"], {"exclues": [], "secondes": 30, "debut": "", "fin": ""})
        self.assertEqual(sortie["bornes"], [10, 300, 30, 30, 45])
        self.assertEqual((sortie["heures"]["debut"], sortie["heures"]["fin"]), ("", "07:30"))
        self.assertEqual(sortie["exclues"], ["cam-a"])

    def test_retenus_dans_le_navigateur(self):
        sortie = executer("""
sortie.lus = reglagesRonde;
reglagesRonde = normaliserReglagesRonde({secondes: 20, exclues: ["cam-b"]});
enregistrerReglagesRonde();
sortie.relus = lireReglagesRonde();
""", stockage={"blink2video.ronde": "{pas du json"})
        self.assertEqual(sortie["lus"]["secondes"], 30)
        self.assertEqual(sortie["relus"],
                         {"exclues": ["cam-b"], "secondes": 20, "debut": "", "fin": ""})

    def test_plage_horaire(self):
        sortie = executer("""
const a = (debut, fin, hhmm) => dansPlageRonde({debut, fin}, new VraieDate(`2026-10-17T${hhmm}:00`));
sortie.r = [a("", "", "03:00"), a("08:00", "", "03:00"), a("08:00", "08:00", "03:00"),
            a("08:00", "20:00", "07:59"), a("08:00", "20:00", "08:00"),
            a("08:00", "20:00", "19:59"), a("08:00", "20:00", "20:00"),
            a("22:00", "06:00", "23:00"), a("22:00", "06:00", "05:59"),
            a("22:00", "06:00", "06:00"), a("22:00", "06:00", "12:00")];
""")
        self.assertEqual(sortie["r"], [True, True, True, False, True, True, False,
                                       True, True, False, False])

    def test_cameras_dans_l_ordre_de_la_grille(self):
        sortie = executer("""
sortie.toutes = camerasRonde(normaliserReglagesRonde(null)).map((c) => c.key);
sortie.choisies = camerasRonde(normaliserReglagesRonde({exclues: ["cam-a"]})).map((c) => c.key);
""", masquees=("D",))
        # C hors ligne, D masquée : ni l'une ni l'autre n'a sa place.
        self.assertEqual(sortie["toutes"], ["cam-a", "cam-b"])
        self.assertEqual(sortie["choisies"], ["cam-b"])


class Enchainement(unittest.TestCase):
    def test_chaque_camera_a_son_temps_a_partir_de_sa_premiere_image(self):
        sortie = executer("""
demarrerRonde();
sortie.plein = document.fullscreenElement === elements.ronde && !elements.ronde.hidden;
sortie.a = {journal: journal.splice(0), dansLaScene: elements["live-cam-a"].parent === elements.rondeScene,
            ancre: cartes["cam-a"].enfants[0].nom, attente: echeance(), titre: elements.rondeTitre.textContent};
surImageRonde({target: elements["live-cam-a"]});
sortie.antenne = echeance();
const minuterie = minuteries[0].id;
// Une reprise du direct rejoue « playing » : le temps d'antenne ne repart pas.
surImageRonde({target: elements["live-cam-a"]});
sortie.uneSeuleFois = minuteries.length === 1 && minuteries[0].id === minuterie;
declencher();
sortie.b = {journal: journal.splice(0), aRendue: elements["live-cam-a"].parent === cartes["cam-a"],
            bDansLaScene: elements["live-cam-b"].parent === elements.rondeScene};
surImageRonde({target: elements["live-cam-b"]});
declencher();
sortie.retour = journal.splice(0);
""", masquees=("D",))
        self.assertTrue(sortie["plein"])
        self.assertEqual(sortie["a"], {"journal": ["watch:cam-a"], "dansLaScene": True,
                                       "ancre": "ancre", "attente": 60000, "titre": "A · 1/2"})
        self.assertEqual(sortie["antenne"], 30000)
        self.assertTrue(sortie["uneSeuleFois"])
        self.assertEqual(sortie["b"], {"journal": ["stop:cam-a", "watch:cam-b"],
                                       "aRendue": True, "bDansLaScene": True})
        # C hors ligne et D masquée sont sautées : la ronde revient à A.
        self.assertEqual(sortie["retour"], ["stop:cam-b", "watch:cam-a"])

    def test_une_image_d_une_autre_case_ne_compte_pas(self):
        sortie = executer("""
demarrerRonde();
surImageRonde({target: elements["live-cam-b"]});
sortie.attente = echeance();
""")
        self.assertEqual(sortie["attente"], 60000)

    def test_un_echec_sans_image_passe_vite_la_main(self):
        sortie = executer("""
demarrerRonde();
promesses["cam-a"]();
await vider();
sortie.apresEchec = echeance();
declencher();
sortie.suivante = journal.slice(-1)[0];
""")
        self.assertEqual(sortie["apresEchec"], 5000)
        self.assertEqual(sortie["suivante"], "watch:cam-b")

    def test_la_fin_du_direct_precedent_ne_presse_pas_le_suivant(self):
        # Passer à B arrête A, dont la promesse se termine juste après : B,
        # encore sans image, garderait sinon 5 s au lieu de 60 pour se réveiller.
        sortie = executer("""
demarrerRonde();
surImageRonde({target: elements["live-cam-a"]});
declencher();
promesses["cam-a"]();
await vider();
sortie.attenteB = echeance();
""")
        self.assertEqual(sortie["attenteB"], 60000)

    def test_hors_plage_la_ronde_attend_sans_reveiller_personne(self):
        sortie = executer("""
maintenant = "2026-10-17T23:00:00";
demarrerRonde();
sortie.nuit = {journal: journal.splice(0), message: elements.rondeMessage.textContent,
               visible: !elements.rondeMessage.hidden, attente: echeance()};
maintenant = "2026-10-17T08:30:00";
declencher();
sortie.matin = journal.splice(0);
maintenant = "2026-10-17T20:00:00";
declencher();
sortie.soir = {journal: journal.splice(0), rendue: elements["live-cam-a"].parent === cartes["cam-a"]};
""", stockage={"blink2video.ronde": json.dumps({"debut": "08:00", "fin": "20:00"})})
        self.assertEqual(sortie["nuit"], {"journal": [], "message": 'ronde.pause{"debut":"08:00"}',
                                          "visible": True, "attente": 30000})
        self.assertEqual(sortie["matin"], ["watch:cam-a"])
        self.assertEqual(sortie["soir"], {"journal": ["stop:cam-a"], "rendue": True})

    def test_sans_camera_la_ronde_le_dit(self):
        sortie = executer("""
demarrerRonde();
sortie.r = {journal: journal, message: elements.rondeMessage.textContent, attente: echeance()};
""", stockage={"blink2video.ronde": json.dumps({"exclues": ["cam-a", "cam-b", "cam-d"]})})
        self.assertEqual(sortie["r"], {"journal": [], "message": "ronde.aucune", "attente": 30000})

    def test_seule_de_sa_ronde_une_camera_garde_l_ecran(self):
        sortie = executer("""
demarrerRonde();
elements["live-cam-a"].video = {paused: false, ended: false, readyState: 4};
surImageRonde({target: elements["live-cam-a"]});
declencher();
sortie.continue = {journal: journal.splice(0), attente: echeance()};
elements["live-cam-a"].video.ended = true;
declencher();
sortie.relance = journal.splice(0);
""", stockage={"blink2video.ronde": json.dumps({"exclues": ["cam-b", "cam-d"]})})
        self.assertEqual(sortie["continue"], {"journal": ["watch:cam-a"], "attente": 30000})
        self.assertEqual(sortie["relance"], ["stop:cam-a", "watch:cam-a"])

    def test_arreter_rend_la_case_et_le_plein_ecran(self):
        sortie = executer("""
demarrerRonde();
journal.splice(0);
arreterRonde();
sortie.r = {journal, rendue: elements["live-cam-a"].parent === cartes["cam-a"],
            cachee: elements.ronde.hidden, minuteries: minuteries.length, active: ronde.active};
""")
        self.assertEqual(sortie["r"], {"journal": ["stop:cam-a", "exitFullscreen", "renderLive"],
                                       "rendue": True, "cachee": True, "minuteries": 0,
                                       "active": False})

    def test_quitter_le_plein_ecran_arrete_la_ronde(self):
        sortie = executer("""
demarrerRonde();
await vider();
surPleinEcranRonde();
document.fullscreenElement = null;
surPleinEcranRonde();
sortie.active = ronde.active;
sortie.journal = journal;
""")
        self.assertFalse(sortie["active"])
        self.assertIn("stop:cam-a", sortie["journal"])

    def test_une_grille_effacee_pendant_le_pret_est_redessinee(self):
        # Une actualisation finie pendant la ronde relit le système et pouvait
        # effacer la grille : sans la redessiner, la ronde restait bloquée sur
        # « aucune caméra ».
        sortie = executer("""
renderLive = () => {
  journal.push("renderLive");
  for (const cle of ["cam-a", "cam-b", "cam-c", "cam-d"]) {
    if (elements["live-" + cle].isConnected) continue;
    cartes[cle] = liste.appendChild(new Noeud("carte-" + cle));
    elements["live-" + cle] = cartes[cle].appendChild(new Noeud("live-" + cle));
  }
};
demarrerRonde();
for (const carte of liste.enfants.slice()) liste.retirer(carte);
declencher();
sortie.r = {journal, bDansLaScene: elements["live-cam-b"].parent === elements.rondeScene,
            scene: elements.rondeScene.enfants.length};
""", masquees=("D",))
        self.assertEqual(sortie["r"], {"journal": ["watch:cam-a", "stop:cam-a", "renderLive", "watch:cam-b"],
                                       "bDansLaScene": True, "scene": 1})

    def test_case_dont_la_carte_a_disparu_n_est_pas_reinseree(self):
        sortie = executer("""
demarrerRonde();
liste.retirer(cartes["cam-a"]);
arreterRonde();
sortie.r = {parent: elements["live-cam-a"].parent, scene: elements.rondeScene.enfants.length};
""")
        self.assertEqual(sortie["r"], {"parent": None, "scene": 0})


class Structure(unittest.TestCase):
    def test_chaque_element_de_la_ronde_existe_dans_la_page(self):
        ids = set(re.findall(r'\$\("(ronde\w*)"\)', SOURCE))
        self.assertTrue({"ronde", "rondeButton", "rondeScene", "rondeReglages"} <= ids, ids)
        for identifiant in sorted(ids):
            self.assertIn(f'id="{identifiant}"', PAGE, identifiant)

    def test_chaque_texte_existe_en_francais_et_en_anglais(self):
        cles = set(re.findall(r'data-i18n(?:-title)?="(ronde\.[\w.]+)"', PAGE))
        cles |= set(re.findall(r'\bt[f]?\("(ronde\.[\w.]+)"', SOURCE))
        self.assertTrue(len(cles) >= 12, cles)
        for cle in sorted(cles):
            self.assertEqual(len(re.findall(rf'"{re.escape(cle)}":', SOURCE)), 2,
                             f"{cle} : attendu une fois en français et une fois en anglais")

    def test_la_ronde_couvre_la_fenetre_et_masque_les_boutons_des_cartes(self):
        self.assertRegex(CSS, r"#ronde \{ position:fixed; inset:0; z-index:100;")
        self.assertIn("#rondeScene .watch { display:none; }", CSS)

    def test_la_grille_ne_se_redessine_pas_pendant_le_pret_d_une_case(self):
        self.assertIn("if (nomsDirectsActifs().length || ronde.boite) return;", SOURCE)
        # loadSystem() annonçait aussi l'interrogation en effaçant la grille.
        self.assertIn('$("view").value === "live" && !nomsDirectsActifs().length && !ronde.boite', SOURCE)


if __name__ == "__main__":
    unittest.main()
