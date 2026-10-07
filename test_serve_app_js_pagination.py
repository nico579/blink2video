from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


class TestsPaginationClips(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node introuvable")
        source = Path(__file__).with_name("serve_app.js").read_text(encoding="utf-8")
        fragments = []
        for nom in ("visible", "duration", "restaurerTaillePageClips", "navigationClips", "changerPageClips", "renderClips", "card", "load", "appliquerFiltre"):
            match = re.search(rf"^(?:async )?function {nom}\(.*?^\}}", source, re.MULTILINE | re.DOTALL)
            if match is None:
                raise AssertionError(f"fonction introuvable : {nom}")
            fragments.append(match.group(0))
        cls.code = "\n".join(fragments)
        cls.constants = re.search(r"^const CLIPS_PAR_PAGE = .*?^let clipsParPage = .*?;", source, re.MULTILINE | re.DOTALL).group(0)
        cls.click = re.search(r'^\$\("list"\).addEventListener\("click", .*?^\}\);', source, re.MULTILINE | re.DOTALL).group(0)
        cls.submit = re.search(r'^\$\("list"\).addEventListener\("submit", .*?^\}\);', source, re.MULTILINE | re.DOTALL).group(0)
        cls.change = re.search(r'^\$\("list"\).addEventListener\("change", .*?^\}\);', source, re.MULTILINE | re.DOTALL).group(0)

    def executer(self, actions="", total=1605, stockage=None, stockage_indisponible=False):
        script = """
(async () => {
const transitions = [];
const lifecycle = [];
const ordre = [];
const stockage = STOCKAGE;
const localStorage = {
  getItem(k) { if (INDISPONIBLE) throw Error('storage'); return stockage[k] ?? null; },
  setItem(k,v) { if (INDISPONIBLE) throw Error('storage'); stockage[k]=v; }
};
const boites = {view:{value:'clips'},camera:{value:''},showOut:{checked:true},
  filtre:{close(){}},count:{},filtreCompte:{},list:{innerHTML:'',scrollIntoView(){},
  querySelector(){return null;},querySelectorAll(){return [];},contains(){return true;},
  addEventListener(n,f){this[n]=f;}}};
const $ = id => boites[id];
const h = v => String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const t = k => k;
const tf = (k,v) => k + ':' + JSON.stringify(v);
const avecJeton = v => v;
let data = {filtered:false,total_known:{clip:1605},suppressionAuto:[],clips:[]};
for(let i=0;i<TOTAL;i++) data.clips.push({kind:'clip',identity:'clip-'+i,
  camera:i%2?'B':'A',cameraKey:'key',day:'2026-09-'+String(30-Math.floor(i/100)).padStart(2,'0'),
  time:'12:00',duration:5,excluded:i%5===0,excludedStaged:false,supprimerStaged:false});
let videos, __chargementEnCours = null, _filtreCameraAppliquee = true;
let nettoyerLecteursClips = () => { lifecycle.push('cleanup'); ordre.push('cleanup'); };
const preparerLecteursClips = () => { lifecycle.push('prepare'); ordre.push('prepare'); };
const _filtrePersiste = null;
let plageClips = {preset:'all'}, plageEnAttente = null;
const paramsPourPlage = () => '';
const fetch = async url => url.startsWith('/api/clips') ? structuredClone(data) : [];
const lireJSON = async answer => answer;
const estMasquee = () => false; const comparerNoms = (a, b) => String(a).localeCompare(String(b));
const fill = () => {}, camerasConnues = () => [], sauvegarderFiltre = () => {};
const majBoutonAppliquer = () => {}, chargerSnapshots = () => {};
const render = () => renderClips();
""".replace("TOTAL", str(total)).replace("STOCKAGE", json.dumps(stockage or {})).replace("INDISPONIBLE", str(stockage_indisponible).lower()) + self.constants + "\n" + self.code + "\n" + self.click + "\n" + self.submit + "\n" + self.change + "\n" + actions + """
renderClips();
console.log(JSON.stringify({html:$('list').innerHTML,page:pageClips,size:clipsParPage,transitions,lifecycle,ordre,stockage,
 selected:data.clips.filter(c=>c.excludedStaged||c.supprimerStaged).map(c=>c.identity)}));
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        r = subprocess.run([self.node, "-e", script], text=True, encoding="utf-8",
                           capture_output=True, timeout=20, check=True)
        return json.loads(r.stdout)

    def identities(self, sortie):
        return re.findall(r'src="/media/clip/([^" ]+)"', sortie["html"])

    def test_premiere_page_bornee_et_ordre_conserve(self):
        sortie = self.executer()
        self.assertEqual(self.identities(sortie), [f"clip-{i}" for i in range(50)])
        self.assertEqual(sortie["html"].count('<div class="clip-player"'), 50)
        self.assertIn('&quot;pages&quot;:33', sortie["html"])
        self.assertEqual(sortie["size"], 50)

    def test_taille_par_le_vrai_gestionnaire_et_retour_premiere_page(self):
        for taille, pages in ((25, 65), (100, 17), (0, 1)):
            sortie = self.executer(f"pageClips=31; $('list').change({{target:{{closest(){{return {{dataset:{{action:'clip-page-size'}},value:'{taille}'}};}}}}}});")
            self.assertEqual(sortie["page"], 0)
            self.assertEqual(sortie["size"], taille)
            self.assertEqual(self.identities(sortie), [f"clip-{i}" for i in range(taille or 1605)])
            if pages > 1:
                self.assertIn(f'&quot;pages&quot;:{pages}', sortie["html"])
            else:
                self.assertNotIn('<nav', sortie["html"])
            self.assertEqual(sortie["stockage"], {"blink2video.clipsParPage": str(taille)})
            self.assertEqual(sortie["html"].count(f'<option value="{taille}" selected>'), 2)

    def test_taille_changee_sur_premiere_page_libere_les_joueurs(self):
        sortie = self.executer("changerPageClips(0, 'size', 100);")
        self.assertEqual(len(self.identities(sortie)), 100)
        self.assertEqual(sortie["lifecycle"].count('cleanup'), 2)
        self.assertEqual(sortie["lifecycle"].count('prepare'), 2)

    def test_derniere_page_pour_chaque_taille(self):
        for taille, page in ((50, 32), (100, 16)):
            sortie = self.executer(f"clipsParPage={taille}; pageClips=999;")
            self.assertEqual(sortie["page"], page)
            self.assertEqual(self.identities(sortie), [f"clip-{i}" for i in range(1600,1605)])

    def test_selecteur_reste_disponible_sur_une_seule_page(self):
        sortie = self.executer("changerPageClips(0, 'size', 100);", total=53)
        self.assertEqual(len(self.identities(sortie)), 53)
        self.assertNotIn("<nav", sortie["html"])
        self.assertEqual(sortie["html"].count('data-action="clip-page-size"'), 2)
        self.assertNotIn('data-action="clip-page"', sortie["html"])
        retour = self.executer("changerPageClips(0, 'size', 100); changerPageClips(0, 'size', 25);", total=53)
        self.assertEqual(len(self.identities(retour)), 25)
        self.assertIn('&quot;pages&quot;:3', retour["html"])

    def test_navigation_par_le_vrai_gestionnaire(self):
        sortie = self.executer("renderClips(); $('list').click({target:{closest(){return {dataset:{action:'clip-page',page:'1',direction:'next'}};}}});")
        self.assertEqual(self.identities(sortie), [f"clip-{i}" for i in range(50,100)])
        self.assertEqual(sortie["page"], 1)
        retour = self.executer("pageClips=1; changerPageClips(0, 'previous');")
        self.assertEqual(self.identities(retour), [f"clip-{i}" for i in range(50)])

    def test_derniere_page_et_bornes(self):
        sortie = self.executer("pageClips=999;")
        self.assertEqual(sortie["page"], 32)
        self.assertEqual(self.identities(sortie), [f"clip-{i}" for i in range(1600,1605)])
        self.assertIn('data-direction="next" disabled', sortie["html"])
        self.assertIn('data-direction="last" disabled', sortie["html"])
        self.assertEqual(self.executer("pageClips=-1;")["page"], 0)

    def test_premiere_et_derniere_par_le_vrai_gestionnaire(self):
        sortie = self.executer("$('list').click({target:{closest(){return {dataset:{action:'clip-page',page:'32',direction:'last'}};}}});")
        self.assertEqual(sortie["page"], 32)
        self.assertEqual(self.identities(sortie), [f"clip-{i}" for i in range(1600,1605)])
        retour = self.executer("pageClips=64; $('list').click({target:{closest(){return {dataset:{action:'clip-page',page:'0',direction:'first'}};}}});")
        self.assertEqual(retour["page"], 0)
        self.assertIn('data-direction="first" disabled', retour["html"])
        self.assertIn('data-direction="previous" disabled', retour["html"])

    def test_saut_aux_pages_32_et_33(self):
        for page in (32, 33):
            sortie = self.executer(f"$('list').submit({{target:{{dataset:{{action:'clip-page-jump'}},elements:{{page:{{valueAsNumber:{page}}}}}}},preventDefault(){{transitions.push('preventDefault');}}}});")
            self.assertEqual(sortie["page"], page-1)
            self.assertEqual(self.identities(sortie), [f"clip-{i}" for i in range((page-1)*50,min(page*50,1605))])
            self.assertEqual(sortie["transitions"], ["preventDefault"])
            self.assertEqual(sortie["html"].count(f'step="1" value="{page}"'), 2)
            self.assertEqual(sortie["html"].count('min="1" max="33"'), 2)

    def test_meme_page_ne_coupe_pas_la_lecture(self):
        sortie = self.executer("$('list').querySelectorAll=()=>[{pause(){transitions.push('pause');}}]; changerPageClips(0);")
        self.assertEqual(sortie["transitions"], [])
        self.assertEqual(sortie["lifecycle"], ['cleanup', 'prepare'])

    def test_focus_du_controle_apres_navigation(self):
        sortie = self.executer("""
$('list').querySelector=()=>({querySelector(selecteur){
  transitions.push(selecteur);
  return selecteur.includes('data-direction') ? null : {focus(){transitions.push('focus');}};
}});
changerPageClips(64, 'last');
""")
        self.assertEqual(sortie["transitions"], ['[data-direction="last"]:not(:disabled)', 'input[name="page"]', 'focus'])

    def test_nettoyage_avant_remplacement_des_cartes(self):
        sortie = self.executer("""
let html='';
Object.defineProperty($('list'),'innerHTML',{get(){return html;},set(v){html=v;transitions.push('replace');ordre.push('replace');}});
changerPageClips(1);
""")
        self.assertEqual(sortie["transitions"], ["replace", "replace"])
        self.assertEqual(sortie["ordre"], ["cleanup", "replace", "prepare", "cleanup", "replace", "prepare"])
        self.assertEqual(sortie["lifecycle"].count('cleanup'), 2)
        self.assertEqual(sortie["lifecycle"].count('prepare'), 2)

    def test_toutes_les_pages_couvrent_la_liste_sans_doublon(self):
        for page in range(2):
            sortie = self.executer(f"pageClips={page};", total=53)
            self.assertEqual(self.identities(sortie), [f"clip-{i}" for i in range(page*50,min((page+1)*50,53))])

    def test_filtre_camera_et_exclusions_avant_pagination(self):
        sortie = self.executer("$('camera').value='A'; $('showOut').checked=false;")
        attendu = [f"clip-{i}" for i in range(1605) if i%2==0 and i%5!=0][:50]
        self.assertEqual(self.identities(sortie), attendu)

    def test_vue_directe_ne_compte_pas_les_detections(self):
        sortie = self.executer("data.clips[0].kind='direct'; $('view').value='direct';")
        self.assertEqual(sortie["html"].count('<div class="clip-player"'), 1)
        self.assertIn('/media/direct/clip-0', sortie["html"])
        self.assertNotIn("<nav", sortie["html"])
        self.assertNotIn('data-action="clip-page-size"', sortie["html"])

    def test_selection_conservee_en_changeant_de_page(self):
        sortie = self.executer("data.clips[0].excludedStaged=true; data.clips[70].supprimerStaged=true; changerPageClips(1); changerPageClips(0);")
        self.assertEqual(sortie["selected"], ['clip-0','clip-70'])
        self.assertIn('type="checkbox" checked', sortie["html"])

    def test_resultats_reduits_et_vides(self):
        sortie = self.executer("pageClips=64; data.clips=data.clips.slice(0,2);")
        self.assertEqual(sortie["page"], 0)
        self.assertEqual(len(self.identities(sortie)), 2)
        sortie = self.executer("pageClips=64; data.clips=[];")
        self.assertEqual(sortie["page"], 0)
        self.assertNotIn('class="clip-player"', sortie["html"])
        self.assertIn('clips.none.ever', sortie["html"])

    def test_groupes_jour_limites_aux_cartes_affichees(self):
        sortie = self.executer("pageClips=1;", total=125)
        self.assertEqual(sortie["html"].count('<h2>'), 1)
        self.assertIn('<h2>2026-09-30</h2>', sortie["html"])


    def test_taille_restauree_au_chargement(self):
        for taille in (25, 50, 100, 0):
            sortie = self.executer(stockage={"blink2video.clipsParPage": str(taille)})
            self.assertEqual(sortie["size"], taille)
            self.assertEqual(len(self.identities(sortie)), taille or 1605)

    def test_taille_inconnue_et_stockage_indisponible(self):
        sortie = self.executer(stockage={"blink2video.clipsParPage": "75"})
        self.assertEqual(sortie["size"], 50)
        sortie = self.executer(stockage_indisponible=True)
        self.assertEqual(len(self.identities(sortie)), 50)
        sortie = self.executer("changerPageClips(0, 'size', 100);", stockage_indisponible=True)
        self.assertEqual(len(self.identities(sortie)), 100)

    def test_tous_puis_retour_aux_pages(self):
        sortie = self.executer("changerPageClips(0, 'size', 0); changerPageClips(0, 'size', 25);")
        self.assertEqual(len(self.identities(sortie)), 25)
        self.assertIn('&quot;pages&quot;:65', sortie["html"])

    def test_aucun_controle_pour_un_historique_court(self):
        for total in (0, 1, 50):
            sortie = self.executer(total=total)
            self.assertNotIn('class="pagination"', sortie["html"])
            self.assertEqual(len(self.identities(sortie)), total)

    def test_actualisation_conserve_page_et_taille(self):
        sortie = self.executer("changerPageClips(12); data.clips.unshift({...data.clips[0],identity:'new'}); await load();")
        self.assertEqual(sortie["page"], 12)
        self.assertEqual(sortie["size"], 50)
        self.assertEqual(self.identities(sortie)[0], 'clip-599')

    def test_actualisation_borne_page_apres_reduction(self):
        sortie = self.executer("changerPageClips(32); data.clips=data.clips.slice(0,103); await load();")
        self.assertEqual(sortie["page"], 2)
        self.assertEqual(self.identities(sortie), ['clip-100', 'clip-101', 'clip-102'])

    def test_appliquer_filtre_revient_a_la_premiere_page(self):
        sortie = self.executer("changerPageClips(12); $('camera').value='B'; await appliquerFiltre();")
        self.assertEqual(sortie["page"], 0)
        self.assertEqual(self.identities(sortie), [f'clip-{i}' for i in range(1,100,2)])


class TestsBarrePagination(unittest.TestCase):
    """La barre « Clips par page » : alignée à gauche, figée sous l'en-tête pendant le
    défilement (capture de Nico du 2026-10-07). Le rendu réel a été vu dans un navigateur ;
    ici, ce qui empêche le retour en arrière."""

    def test_barre_a_gauche_et_figee_sous_l_entete(self):
        css = Path(__file__).with_name("serve_style.css").read_text(encoding="utf-8")
        regle = re.search(r"\.pagination \{([^}]*)\}", css).group(1)
        self.assertIn("justify-content:flex-start", regle)
        self.assertNotIn("center", regle.split("justify-content:")[1].split(";")[0])
        figee = re.search(r"#list > \.pagination:first-child \{([^}]*)\}", css).group(1)
        self.assertIn("position:sticky", figee)
        self.assertIn("top:var(--entete-h", figee)

    def test_la_hauteur_de_l_entete_est_publiee_en_variable_css(self):
        js = Path(__file__).with_name("serve_app.js").read_text(encoding="utf-8")
        self.assertIn('setProperty("--entete-h"', js)
        self.assertIn("ResizeObserver", js)


if __name__ == "__main__":
    unittest.main()
