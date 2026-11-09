"""Issue #40, réglages promis à MCas86 le 2026-10-19.

Le titre et l'icône de l'en-tête, réglés une fois pour l'installation et
insérés dans la page à chaque envoi ; la dernière vue retenue par navigateur,
et la ronde qui reprend quand seule la fermeture de la page l'avait coupée.
Le tri des caméras hors ligne est dans test_serve_app_js_confort_cameras.py.
Côté page, le vrai code de serve_app.js, exécuté par node avec un faux DOM.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

_IMPORT_HOME = tempfile.TemporaryDirectory(prefix="blink-entete-import-")
with mock.patch.dict(os.environ, {"BLINK_BOOTSTRAP": "none",
                                  "BLINK_HOME": _IMPORT_HOME.name}):
    import serve

import runtime

RACINE = Path(__file__).parent
SOURCE = (RACINE / "serve_app.js").read_text(encoding="utf-8")


def entete(page: str) -> tuple[str, str, str]:
    titre = re.search(r"<title>(.*?)</title>", page).group(1)
    favicon = re.search(r'<link rel="icon" href="([^"]*)">', page).group(1)
    h1 = re.search(r"<h1>(.*?)<span class=\"v\">", page).group(1)
    return titre, favicon, h1


class EnteteDeLaPage(unittest.TestCase):
    def test_sans_reglage_la_page_d_avant(self):
        titre, favicon, h1 = entete(serve.page_avec_entete(dict(runtime.REGLAGES_DEFAUT)))
        self.assertEqual((titre, favicon, h1), ("blink2video", "/favicon.ico", "blink2video"))

    def test_titre_et_icone_dans_l_en_tete_et_l_onglet(self):
        page = serve.page_avec_entete({"header_title": "Maison", "header_icon": "🏠"})
        titre, favicon, h1 = entete(page)
        self.assertEqual(titre, "Maison")
        self.assertEqual(h1, '<span class="icone">🏠</span> Maison')
        self.assertTrue(favicon.startswith("data:image/svg+xml,"))
        self.assertIn("🏠", unquote(favicon))
        # La CSP de la page doit laisser passer cette icône d'onglet.
        self.assertIn("img-src 'self' data:", Path(serve.__file__).read_text(encoding="utf-8"))

    def test_icone_seule_garde_le_nom_de_l_application(self):
        titre, _, h1 = entete(serve.page_avec_entete({"header_title": "", "header_icon": "🚗"}))
        self.assertEqual((titre, h1), ("blink2video", '<span class="icone">🚗</span> blink2video'))

    def test_texte_echappe_et_marqueurs_laisses_tels_quels(self):
        page = serve.page_avec_entete({"header_title": '<script>x</script> & "__FAVICON__"',
                                       "header_icon": '"><b>'})
        titre, favicon, h1 = entete(page)
        self.assertEqual(titre, "&lt;script&gt;x&lt;/script&gt; &amp; &quot;__FAVICON__&quot;")
        self.assertNotIn("<script>x", page)
        self.assertNotIn("<b>", page)
        self.assertNotIn('"', favicon)
        self.assertIn("__FAVICON__", h1)

    def test_aucun_marqueur_ne_reste_dans_la_page_envoyee(self):
        page = serve.page_avec_entete({"header_title": "A", "header_icon": "B"})
        self.assertIsNone(re.search(r"__(TITRE_PAGE|FAVICON|ENTETE)__", page))

    def test_chaque_champ_existe_dans_la_page_et_les_deux_langues(self):
        modele = Path(serve.__file__).read_text(encoding="utf-8")
        for identifiant in ("headerTitle", "headerIcon"):
            self.assertIn(f'id="{identifiant}"', modele)
        for cle in ("reglages.entete", "reglages.entete.hint", "reglages.entete.titre",
                    "reglages.entete.icone"):
            self.assertEqual(SOURCE.count(f'"{cle}":'), 2, cle)


class EnteteDansLeFichierDeReglages(unittest.TestCase):
    def setUp(self):
        temporaire = tempfile.TemporaryDirectory(prefix="blink-entete-")
        self.addCleanup(temporaire.cleanup)
        self.dossier = Path(temporaire.name)
        patch = mock.patch.object(runtime, "app_dir", return_value=self.dossier)
        patch.start()
        self.addCleanup(patch.stop)

    def test_aller_retour(self):
        reglages = dict(runtime.REGLAGES_DEFAUT, header_title="Garage", header_icon="🚗")
        runtime.ecrire_reglages(**reglages)
        relus = runtime.lire_reglages()
        self.assertEqual((relus["header_title"], relus["header_icon"]), ("Garage", "🚗"))

    def test_fichier_ancien_ou_retouche_a_la_main(self):
        fichier = self.dossier / runtime.REGLAGES
        fichier.write_text(json.dumps({"port": 8765}), encoding="utf-8")
        relus = runtime.lire_reglages()
        self.assertEqual((relus["header_title"], relus["header_icon"]), ("", ""))
        fichier.write_text(json.dumps({"header_title": "x" * 99 + "\t", "header_icon": 7}),
                           encoding="utf-8")
        relus = runtime.lire_reglages()
        self.assertEqual((relus["header_title"], relus["header_icon"]), ("x" * 60, ""))


def extraire(motif: str) -> str:
    trouve = re.search(motif, SOURCE, re.DOTALL | re.MULTILINE)
    if trouve is None:
        raise AssertionError(f"introuvable dans serve_app.js : {motif}")
    return trouve.group(0)


CODE_PAGE = "\n".join([
    extraire(r"^const CLE_VUE = .*?;$"),
    extraire(r"^function restaurerVue\(.*?^\}"),
    extraire(r"^function memoriserVue\(.*?^\}"),
    extraire(r"^let rondeAReprendre = .*?;$"),
    extraire(r"^async function loadSystem\(.*?^\}"),
])

MONTAGE = r"""
const magasin = STOCKAGE;
globalThis.localStorage = {
  getItem: (c) => (c in magasin ? magasin[c] : null),
  setItem: (c, v) => { magasin[c] = String(v); },
};
const options = ["live", "direct", "clips", "daily", "weekly", "monthly", "pictures"]
  .map((value) => ({ value }));
const elements = { view: { value: "clips", options }, list: { innerHTML: "" },
                   count: { textContent: "" } };
function $(id) { return elements[id]; }
const t = (cle) => cle;
let system = null;
const ronde = { boite: null };
const nomsDirectsActifs = () => [];
const journal = [];
function renderLive() { journal.push("renderLive"); }
function demarrerRonde() { journal.push("demarrerRonde"); }
let reponseSysteme = SYSTEME;
globalThis.fetch = async () => ({});
async function lireJSON() {
  if (reponseSysteme instanceof Error) throw reponseSysteme;
  return reponseSysteme;
}
"""


def executer(scenario: str, stockage: dict | None = None, systeme=None) -> dict:
    node = shutil.which("node")
    if node is None:
        raise unittest.SkipTest("node introuvable")
    montage = (MONTAGE.replace("STOCKAGE", json.dumps(stockage or {}))
               .replace("SYSTEME", json.dumps(systeme or {"systems": []})))
    script = (montage + CODE_PAGE + "\nconst sortie = {};\n(async () => {\n" + scenario
              + "\nconsole.log(JSON.stringify(sortie));\n})();\n")
    resultat = subprocess.run([node, "-"], input=script, capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
    assert resultat.returncode == 0, resultat.stderr
    return json.loads(resultat.stdout)


class DerniereVue(unittest.TestCase):
    def test_retenue_puis_rouverte(self):
        sortie = executer("""
sortie.premiere = restaurerVue();
memoriserVue("live");
sortie.relue = restaurerVue();
""")
        self.assertEqual((sortie["premiere"], sortie["relue"]), ("clips", "live"))

    def test_vue_inconnue_ou_abimee_retour_aux_clips(self):
        for abime in ("ronde", "", "__proto__", "constructor"):
            with self.subTest(abime=abime):
                sortie = executer("sortie.vue = restaurerVue();", {"blink2video.vue": abime})
                self.assertEqual(sortie["vue"], "clips")

    def test_stockage_indisponible(self):
        sortie = executer("""
globalThis.localStorage = { getItem() { throw new Error("bloqué"); },
                            setItem() { throw new Error("bloqué"); } };
memoriserVue("live");
sortie.vue = restaurerVue();
""")
        self.assertEqual(sortie["vue"], "clips")

    def test_vue_posee_avant_le_premier_rendu_et_retenue_au_changement(self):
        # Menu et contenu doivent rester d'accord (bug « repassait en Direct
        # toute seule ») : la vue restaurée est posée avant setLang(), qui rend.
        pose = SOURCE.index('$("view").value = restaurerVue();')
        self.assertLess(pose, SOURCE.index("setLang(localStorage.getItem(\"lang\")"))
        changement = extraire(r'^\$\("view"\)\.onchange = \(\) => \{.*?^\};')
        self.assertIn('memoriserVue($("view").value);', changement)


class RepriseDeLaRonde(unittest.TestCase):
    def test_reprend_une_fois_les_cameras_connues(self):
        sortie = executer("""
elements.view.value = "live";
rondeAReprendre = true;
await loadSystem(false);
sortie.journal = journal.splice(0);
sortie.encore = rondeAReprendre;
// Une relecture du système plus tard ne relance rien.
await loadSystem(true);
sortie.ensuite = journal.splice(0);
""")
        self.assertEqual(sortie["journal"], ["renderLive", "demarrerRonde"])
        self.assertFalse(sortie["encore"])
        self.assertEqual(sortie["ensuite"], ["renderLive"])

    def test_pas_de_reprise_sur_erreur_ni_hors_du_direct(self):
        sortie = executer("""
elements.view.value = "live";
rondeAReprendre = true;
reponseSysteme = new Error("hors ligne");
await loadSystem(false);
sortie.erreur = journal.splice(0);
elements.view.value = "clips";
system = null;
reponseSysteme = {systems: []};
await loadSystem(false);
sortie.ailleurs = journal.splice(0);
sortie.attend = rondeAReprendre;
""")
        self.assertEqual(sortie["erreur"], ["renderLive"])
        self.assertEqual(sortie["ailleurs"], [])
        self.assertTrue(sortie["attend"])

    def test_ni_derriere_la_connexion_ni_derriere_la_configuration(self):
        decision = extraire(r"^rondeAReprendre = .*?;$")
        self.assertIn('$("view").value === "live"', decision)
        self.assertIn("rondeEtaitEnCours()", decision)
        self.assertIn("(login|setup)=1", decision)


if __name__ == "__main__":
    unittest.main()
