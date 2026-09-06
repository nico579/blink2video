"""Bug rapporté le 2026-09-06 (côté client) : stopWatch() ne signalait
jamais explicitement au serveur qu'un direct MSE devait s'arrêter - seul
l'AbortController local (MSE_ABORT) était coupé, la connexion HTTP restant
ouverte côté serveur jusqu'à sa prochaine écriture (voir le pendant serveur,
test_serve_live_mse_arret_explicite.py)."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


class TestsArretExpliciteDirectMseCoteClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node introuvable")
        source = (Path(__file__).parent / "serve_app.js").read_text(encoding="utf-8")
        debut = source.index("function arreterSessionDirect")
        fin = source.index("// --- MSE/fMP4", debut)
        cls.bloc = source[debut:fin]
        if "arreterSessionDirect(mseSessionId, beacon)" not in cls.bloc:
            raise AssertionError("stopWatch() n'appelle plus arreterSessionDirect pour MSE")

    def _executer(self, scenario: str) -> dict:
        script = f"""
{self.bloc}

const fetchAppels = [];
global.fetch = (url, options) => {{
  fetchAppels.push({{ url, options }});
  return Promise.resolve({{ ok: true }});
}};
global.navigator = {{ sendBeacon: () => false }};
const BLINK_TOKEN = "jeton-test";

const boxes = {{}};
function $(id) {{ return boxes[id] || null; }}
function cssId(name) {{ return name.replace(/[^\\w-]/g, "_"); }}
function repos(name, libelle) {{ return `repos:${{name}}:${{libelle}}`; }}
function t(cle) {{ return cle; }}

const LIVE_PENDING = {{}};
const MSE_ABORT = {{}};
const MSE_SESSION = {{}};
const WEBRTC_ABORT = {{}};
const WEBRTC_SESSION = {{}};
const WEBRTC_PC = {{}};

{scenario}

process.stdout.write(JSON.stringify({{
  fetchAppels: fetchAppels.map((a) => ({{
    url: a.url, method: a.options && a.options.method,
    body: a.options && a.options.body ? JSON.parse(a.options.body) : null,
  }})),
  mseSessionRestante: MSE_SESSION["Jardin"] || null,
  webrtcSessionRestante: WEBRTC_SESSION["Jardin"] || null,
}}));
"""
        resultat = subprocess.run(
            [self.node, "-e", script], capture_output=True, text=True, timeout=10,
        )
        if resultat.returncode != 0:
            self.fail(f"Node a échoué : {resultat.stderr}")
        return json.loads(resultat.stdout)

    def test_stop_watch_signale_l_arret_explicite_d_une_session_mse(self):
        resultat = self._executer("""
MSE_SESSION["Jardin"] = "session-mse-abc";
MSE_ABORT["Jardin"] = { abort() {} };
stopWatch("Jardin");
""")
        self.assertEqual(len(resultat["fetchAppels"]), 1)
        appel = resultat["fetchAppels"][0]
        self.assertEqual(appel["url"], "/api/arreter-direct")
        self.assertEqual(appel["method"], "POST")
        self.assertEqual(appel["body"], {"session_id": "session-mse-abc"})
        self.assertIsNone(resultat["mseSessionRestante"])

    def test_stop_watch_sans_session_mse_ne_fait_pas_d_appel_inutile(self):
        resultat = self._executer("""
stopWatch("Jardin");
""")
        self.assertEqual(resultat["fetchAppels"], [])

    def test_stop_watch_signale_mse_et_webrtc_independamment(self):
        resultat = self._executer("""
MSE_SESSION["Jardin"] = "session-mse";
WEBRTC_SESSION["Jardin"] = "session-webrtc";
stopWatch("Jardin");
""")
        session_ids = {a["body"]["session_id"] for a in resultat["fetchAppels"]}
        self.assertEqual(session_ids, {"session-mse", "session-webrtc"})
        self.assertIsNone(resultat["mseSessionRestante"])
        self.assertIsNone(resultat["webrtcSessionRestante"])


class TestsGenerationSessionIdWatchMse(unittest.TestCase):
    """watchMse() doit poser MSE_SESSION[name] AVANT son premier appel à
    connecterMse() (sinon un clic sur Arrêter juste après le lancement du
    direct ne trouverait rien à annuler), et le transmettre tel quel."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node introuvable")
        source = (Path(__file__).parent / "serve_app.js").read_text(encoding="utf-8")
        debut_id = source.index("function nouvelIdentifiantDirect")
        fin_id = source.index("\n}\n", debut_id) + len("\n}\n")
        debut_watch = source.index("async function watchMse")
        fin_watch = source.index("// L'état de repos", debut_watch)
        cls.bloc = source[debut_id:fin_id] + "\n" + source[debut_watch:fin_watch]
        if "connecterMse(" not in cls.bloc or "sessionId" not in cls.bloc:
            raise AssertionError("watchMse() ne transmet plus de sessionId à connecterMse()")

    def test_sessionid_pose_avant_le_premier_appel_et_transmis_tel_quel(self):
        script = f"""
global.window = {{}};

{self.bloc}

const boxes = {{
  "live-Jardin": {{ innerHTML: "", querySelector: () => ({{}}) }},
}};
function $(id) {{ return boxes[id] || null; }}
function h(s) {{ return s; }}
function cssId(name) {{ return name.replace(/[^\\w-]/g, "_"); }}
function t(cle) {{ return cle; }}
function recordBtn() {{ return ""; }}
function expandBtn() {{ return ""; }}
function failWatch() {{}}
function stopWatch() {{}}
function attendreOuAbandon() {{ return Promise.resolve(); }}
const MSE_ABORT = {{}};
const MSE_SESSION = {{}};
const MSE_MAX_ECHECS_A_VIDE = 1;
const MSE_BUDGET_TOTAL_MS = 10000;
const MSE_DELAI_MODULE_OCCUPE_MS = 1;
const MSE_DELAI_RECONNEXION_MS = 1;

let sessionIdVuParConnecterMse = null;
let sessionIdPoseAvantAppel = null;
async function connecterMse(name, video, signal, texte, t0, reveilInitial, sessionId) {{
  sessionIdVuParConnecterMse = sessionId;
  sessionIdPoseAvantAppel = MSE_SESSION[name];
  const error = new Error("Aborted");
  error.name = "AbortError";
  throw error;
}}

watchMse("Jardin").then(() => {{
  process.stdout.write(JSON.stringify({{
    sessionIdVuParConnecterMse,
    sessionIdPoseAvantAppel,
    sessionIdApresRetour: MSE_SESSION["Jardin"] || null,
    coherent: sessionIdVuParConnecterMse === sessionIdPoseAvantAppel
      && typeof sessionIdVuParConnecterMse === "string"
      && sessionIdVuParConnecterMse.length > 0,
  }}));
}});
"""
        resultat = subprocess.run(
            [self.node, "-e", script], capture_output=True, text=True, timeout=10,
        )
        if resultat.returncode != 0:
            self.fail(f"Node a échoué : {resultat.stderr}")
        donnees = json.loads(resultat.stdout)
        self.assertTrue(
            donnees["coherent"],
            f"sessionId incohérent ou absent : {donnees}",
        )
        # Le "finally" de watchMse() nettoie toujours MSE_SESSION à sa
        # sortie, quelle qu'en soit la raison (échec ici) - seul l'instant
        # T pendant l'appel à connecterMse() (vérifié ci-dessus par
        # "coherent") doit porter le sessionId.
        self.assertIsNone(donnees["sessionIdApresRetour"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
