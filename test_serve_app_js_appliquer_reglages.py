"""Exerce le formulaire réel et ses trois attentes avec un DOM et une horloge simulés."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


class TestsApplicationReglagesJS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node introuvable")
        source = (Path(__file__).parent / "serve_app.js").read_text(encoding="utf-8")
        debut = source.index("// Même déroulé que le bouton de mise à jour")
        fin = source.index('$("stopButton").onclick', debut)
        lire_json = re.search(
            r"^async function lireJSON\(.*?^\}", source, re.DOTALL | re.MULTILINE,
        )
        if lire_json is None:
            raise AssertionError("fonction lireJSON() introuvable")
        cls.source = lire_json.group(0) + "\n" + source[debut:fin]

    def _executer(self, **params):
        script = r"""
const params = JSON.parse(process.argv[1]);
const requetes = [], alertes = [], delais = [], etapes = [];
let fermetures = 0, rechargements = 0, identifiant = 0;
const intervalles = new Map(), temporisations = new Map();
globalThis.setInterval = (fonction, delai) => {
  const id = ++identifiant;
  intervalles.set(id, {fonction, delai});
  delais.push({type: 'interval', delai});
  return id;
};
globalThis.clearInterval = (id) => { intervalles.delete(id); };
globalThis.setTimeout = (fonction, delai) => {
  const id = ++identifiant;
  temporisations.set(id, {fonction, delai});
  delais.push({type: 'timeout', delai});
  return id;
};
globalThis.clearTimeout = (id) => { temporisations.delete(id); };
const elements = Object.fromEntries(Object.entries({
  usbMinutes: '5', cloudMinutes: '15', port: '5000', timezone: 'Europe/Paris',
  storageDir: ' clips ', liveProtocol: 'mse',
}).map(([id, value]) => [id, {value}]));
for (const [id, checked] of Object.entries({
  timestamp: true, mergeJour: true, mergeSemaine: false,
  mergeMois: true, downloadAuto: false,
})) elements[id] = {checked};
for (const [id, value] of Object.entries(params.valeurs || {})) elements[id].value = value;
for (const [id, checked] of Object.entries(params.coches || {})) elements[id].checked = checked;
elements.reglagesApply = {disabled: false, textContent: 'reglages.apply'};
elements.reglages = {close: () => { fermetures += 1; }};
elements.phase = {textContent: 'prêt'};
elements.bar = {
  value: 25, indetermine: false,
  removeAttribute: (nom) => {
    if (nom !== 'value') throw new Error(`Attribut inattendu : ${nom}`);
    elements.bar.indetermine = true;
    elements.bar.value = 0;
  },
};
const classes = new Set();
elements.work = {classList: {add: (nom) => { classes.add(nom); }}};
elements.refresh = {disabled: false};
const $ = (id) => {
  if (!(id in elements)) throw new Error(`Élément inattendu : ${id}`);
  return elements[id];
};
const portActuel = 5000;
const t = (cle) => cle;
const tf = (cle, valeurs) => `${cle}:${JSON.stringify(valeurs)}`;
globalThis.alert = (message) => { alertes.push(message); };
globalThis.location = {
  hostname: 'camera.local', pathname: '/galerie/', search: '?setup=1',
  href: 'http://camera.local:5000/galerie/?setup=1',
  reload: () => { rechargements += 1; },
};
let libererPost;
const attentePost = new Promise((resolve) => { libererPost = resolve; });
const statuts = [...(params.statuts || [])];
globalThis.fetch = async (url, options) => {
  requetes.push({url, options: options || null});
  if (url === '/api/reglages') {
    await attentePost;
    if (params.post === 'reseau') throw new TypeError('Connexion interrompue');
    return {json: async () => {
      if (params.post === 'json') throw new SyntaxError('JSON interrompu');
      return params.reponse || {ok: true};
    }};
  }
  if (url !== '/api/status') throw new Error(`Requête inattendue : ${url}`);
  const statut = statuts.length ? statuts.shift() : {initial_setup: true};
  if (statut === 'reseau') throw new TypeError('Serveur arrêté');
  return {json: async () => {
    if (statut === 'json') throw new SyntaxError('Statut incomplet');
    return statut;
  }};
};
function instantane() {
  return {
    bouton: {...elements.reglagesApply}, fermetures, rechargements,
    phase: elements.phase.textContent,
    barre: {value: elements.bar.value, indetermine: elements.bar.indetermine},
    travail: classes.has('on'), refreshBloque: elements.refresh.disabled,
    adresse: location.href,
    intervalles: [...intervalles.values()].map((timer) => timer.delai),
    temporisations: [...temporisations.values()].map((timer) => timer.delai),
  };
}
""" + self.source + r"""
(async () => {
  const application = $('reglagesApply').onclick();
  const pendantPost = instantane();
  libererPost();
  await application;
  const apresPost = instantane();
  for (const action of params.actions || []) {
    if (action === 'poll') {
      const timers = [...intervalles.values()];
      if (timers.length !== 1) throw new Error(`Sondages actifs : ${timers.length}`);
      await timers[0].fonction();
    } else {
      const timer = [...temporisations.entries()].find(([, item]) => item.delai === action);
      if (!timer) throw new Error(`Temporisation absente : ${action}`);
      temporisations.delete(timer[0]);
      await timer[1].fonction();
    }
    etapes.push(instantane());
  }
  process.stdout.write(JSON.stringify({requetes, alertes, delais, pendantPost, apresPost, etapes}));
})().catch((erreur) => { console.error(erreur); process.exitCode = 1; });
"""
        resultat = subprocess.run(
            [self.node, "-e", script, json.dumps(params)], capture_output=True,
            text=True, encoding="utf-8", check=True, timeout=30,
        )
        return json.loads(resultat.stdout)

    def _verifier_attente(self, etat, intervalle, butoir):
        self.assertEqual(etat["bouton"], {"disabled": False, "textContent": "reglages.apply"})
        self.assertEqual(etat["fermetures"], 1)
        self.assertTrue(etat["travail"])
        self.assertTrue(etat["refreshBloque"])
        self.assertTrue(etat["barre"]["indetermine"])
        self.assertEqual(etat["intervalles"], [intervalle])
        self.assertEqual(etat["temporisations"], [butoir])

    def _verifier_echec_attente(self, etat):
        self.assertEqual(etat["phase"], "reglages.restartFailed")
        self.assertEqual(etat["barre"]["value"], 0)
        self.assertFalse(etat["refreshBloque"])
        self.assertEqual(etat["intervalles"], [])
        self.assertEqual(etat["rechargements"], 0)
        self.assertEqual(etat["adresse"], "http://camera.local:5000/galerie/?setup=1")

    def test_validation_ne_poste_pas_et_ne_lance_aucune_attente(self):
        cas = [
            ({"usbMinutes": "0"}, "cadence"),
            ({"usbMinutes": ""}, "cadence"),
            ({"cloudMinutes": "-1"}, "cadence"),
            ({"cloudMinutes": "abc"}, "cadence"),
            ({"port": "0"}, "port"),
            ({"port": "65536"}, "port"),
            ({"port": ""}, "port"),
            ({"timezone": " \t "}, "timezone"),
            ({"usbMinutes": "0", "port": "0", "timezone": ""}, "cadence"),
            ({"port": "0", "timezone": ""}, "port"),
        ]
        for valeurs, erreur in cas:
            with self.subTest(valeurs=valeurs):
                resultat = self._executer(valeurs=valeurs)
                self.assertEqual(resultat["alertes"], [f"reglages.error.{erreur}"])
                self.assertEqual(resultat["requetes"], [])
                self.assertEqual(resultat["delais"], [])
                self.assertEqual(resultat["apresPost"]["fermetures"], 0)
                self.assertEqual(resultat["apresPost"]["bouton"], {
                    "disabled": False, "textContent": "reglages.apply",
                })

    def test_payload_normalise_et_bouton_bloque_pendant_le_post(self):
        resultat = self._executer(valeurs={
            "usbMinutes": " 003.7 minutes", "cloudMinutes": "12abc", "port": "05000",
            "timezone": "  Europe/Paris\n", "storageDir": "  C:\\Mes vidéos\\  ",
        })
        self.assertEqual(resultat["pendantPost"]["bouton"], {
            "disabled": True, "textContent": "reglages.restarting",
        })
        self.assertEqual(resultat["pendantPost"]["fermetures"], 0)
        self.assertEqual(resultat["pendantPost"]["intervalles"], [])
        post, = resultat["requetes"]
        self.assertEqual(post["url"], "/api/reglages")
        self.assertEqual(post["options"]["method"], "POST")
        self.assertEqual(post["options"]["headers"], {"Content-Type": "application/json"})
        self.assertEqual(json.loads(post["options"]["body"]), {
            "usb_minutes": 3, "cloud_minutes": 12, "port": 5000,
            "storage_dir": "C:\\Mes vidéos\\", "timezone": "Europe/Paris",
            "timestamp": True, "live_protocol": "mse", "merge_jour": True,
            "merge_semaine": False, "merge_mois": True, "download_auto": False,
        })
        self._verifier_attente(resultat["apresPost"], 2000, 45000)
        self.assertEqual(resultat["alertes"], [])

    def test_booleens_et_protocole_webrtc_transmis_sans_conversion(self):
        resultat = self._executer(
            valeurs={"liveProtocol": "webrtc", "storageDir": "   "},
            coches={"timestamp": False, "mergeJour": False, "mergeSemaine": True,
                    "mergeMois": False, "downloadAuto": True},
        )
        payload = json.loads(resultat["requetes"][0]["options"]["body"])
        self.assertEqual(payload["storage_dir"], "")
        self.assertEqual(payload["live_protocol"], "webrtc")
        self.assertEqual([payload[cle] for cle in (
            "timestamp", "merge_jour", "merge_semaine", "merge_mois", "download_auto",
        )], [False, False, True, False, True])

    def test_refus_json_garde_le_formulaire_ouvert_sans_sondage(self):
        resultat = self._executer(reponse={"error": "Réglage refusé", "initial_setup": True})
        self.assertEqual(resultat["alertes"], ["Réglage refusé"])
        self.assertEqual(resultat["delais"], [])
        self.assertEqual(resultat["apresPost"]["fermetures"], 0)
        self.assertEqual(resultat["apresPost"]["bouton"], {
            "disabled": False, "textContent": "reglages.apply",
        })
        self.assertFalse(resultat["apresPost"]["travail"])

    def test_reponse_interrompue_lance_quand_meme_attente(self):
        for mode in ("reseau", "json"):
            with self.subTest(mode=mode):
                resultat = self._executer(post=mode)
                self._verifier_attente(resultat["apresPost"], 2000, 45000)
                self.assertEqual(resultat["alertes"], [])
                self.assertEqual(resultat["apresPost"]["rechargements"], int(mode == "json"))

    def test_redemarrage_ordinaire_attend_arret_puis_retour(self):
        resultat = self._executer(
            statuts=[{}, "reseau", "reseau", {}], actions=["poll"] * 4,
        )
        self._verifier_attente(resultat["apresPost"], 2000, 45000)
        self.assertEqual(resultat["apresPost"]["phase"], "reglages.restarting.settings")
        self.assertEqual([etat["rechargements"] for etat in resultat["etapes"]], [0, 0, 0, 1])
        for requete in resultat["requetes"][1:]:
            self.assertEqual(requete, {"url": "/api/status", "options": {"cache": "no-store"}})

    def test_redemarrage_ordinaire_expire_si_serveur_ne_disparait_pas(self):
        resultat = self._executer(statuts=[{}], actions=["poll", 45000])
        self._verifier_echec_attente(resultat["etapes"][-1])

    def test_redemarrage_ordinaire_garde_attente_apres_disparition_et_butoir(self):
        resultat = self._executer(statuts=["reseau", {}], actions=["poll", 45000, "poll"])
        apres_butoir = resultat["etapes"][1]
        self.assertEqual(apres_butoir["intervalles"], [2000])
        self.assertEqual(apres_butoir["phase"], "reglages.restarting.settings")
        self.assertTrue(apres_butoir["refreshBloque"])
        self.assertEqual(resultat["etapes"][-1]["rechargements"], 1)

    def test_changement_port_prioritaire_sur_configuration_initiale(self):
        resultat = self._executer(
            valeurs={"port": "6001"}, reponse={"ok": True, "initial_setup": True},
            statuts=[{}, "reseau"], actions=["poll", "poll", 45000, 3000],
        )
        self._verifier_attente(resultat["apresPost"], 1000, 45000)
        self.assertEqual(resultat["apresPost"]["phase"],
                         'reglages.portchange:{"url":"http://camera.local:6001/"}')
        self.assertEqual(resultat["etapes"][0]["intervalles"], [1000])
        self.assertEqual(resultat["etapes"][1]["intervalles"], [])
        self.assertIn(3000, resultat["etapes"][1]["temporisations"])
        self.assertTrue(resultat["etapes"][2]["refreshBloque"])
        self.assertEqual(resultat["etapes"][-1]["adresse"], "http://camera.local:6001/")
        self.assertEqual(resultat["etapes"][-1]["rechargements"], 0)

    def test_changement_port_expire_si_ancienne_origine_reste_active(self):
        resultat = self._executer(valeurs={"port": "6001"}, actions=["poll", 45000])
        self._verifier_echec_attente(resultat["etapes"][-1])
        self.assertNotIn({"type": "timeout", "delai": 3000}, resultat["delais"])

    def test_configuration_initiale_attend_false_strict_puis_retire_query(self):
        resultat = self._executer(
            reponse={"ok": True, "initial_setup": True},
            statuts=[{"initial_setup": True}, {}, {"initial_setup": "false"},
                     {"initial_setup": 0}, {"initial_setup": False}],
            actions=["poll"] * 5,
        )
        self._verifier_attente(resultat["apresPost"], 1000, 60000)
        for etat in resultat["etapes"][:-1]:
            self.assertEqual(etat["adresse"], "http://camera.local:5000/galerie/?setup=1")
            self.assertEqual(etat["intervalles"], [1000])
        final = resultat["etapes"][-1]
        self.assertEqual(final["adresse"], "/galerie/")
        self.assertEqual(final["intervalles"], [])
        self.assertEqual(final["temporisations"], [])
        self.assertEqual(final["rechargements"], 0)

    def test_configuration_initiale_tolere_coupures_et_json_incomplet(self):
        resultat = self._executer(
            reponse={"initial_setup": True}, statuts=["reseau", "json", {"initial_setup": False}],
            actions=["poll"] * 3,
        )
        self.assertEqual(resultat["alertes"], [])
        self.assertEqual([etat["rechargements"] for etat in resultat["etapes"]], [0, 0, 0])
        self.assertEqual(resultat["etapes"][-1]["adresse"], "/galerie/")

    def test_configuration_initiale_expire_meme_apres_une_coupure(self):
        resultat = self._executer(
            reponse={"initial_setup": True}, statuts=["reseau"], actions=["poll", 60000],
        )
        self._verifier_echec_attente(resultat["etapes"][-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
