"""Régressions du lecteur MSE exécutées sur le vrai JavaScript.

Le navigateur fournit MediaSource, SourceBuffer et ReadableStream. Ce test les
remplace par de petits objets déterministes sous Node afin de vérifier les
timeouts et le nettoyage sans caméra Blink ni ffmpeg réels. Il est sauté sur
les machines qui n'ont pas Node, comme les autres tests de ``serve_app.js``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class TestsLecteurMse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node introuvable")

        source = (Path(__file__).parent / "serve_app.js").read_text(encoding="utf-8")
        debut = source.index("const MSE_ABORT = {};")
        fin = source.index("// L'état de repos", debut)
        bloc = source[debut:fin]

        # Les mêmes délais, raccourcis mécaniquement pour rendre les blocages
        # reproductibles en quelques millisecondes dans le faux navigateur.
        remplacements = {
            "const MSE_DELAI_RECONNEXION_MS = 3000;":
                "const MSE_DELAI_RECONNEXION_MS = 3;",
            "const MSE_DELAI_MODULE_OCCUPE_MS = 10000;":
                "const MSE_DELAI_MODULE_OCCUPE_MS = 5;",
            "const MSE_BUDGET_TOTAL_MS = 10 * 60 * 1000;":
                "const MSE_BUDGET_TOTAL_MS = 70;",
            "const MSE_DELAI_SOURCEOPEN_MS = 10 * 1000;":
                "const MSE_DELAI_SOURCEOPEN_MS = 15;",
            "const MSE_DELAI_REPONSE_MS = 110 * 1000;":
                "const MSE_DELAI_REPONSE_MS = 25;",
            "const MSE_DELAI_PREMIERE_IMAGE_MS = 60 * 1000;":
                "const MSE_DELAI_PREMIERE_IMAGE_MS = 25;",
            "const MSE_DUREE_APRES_LECTURE_MS = 330 * 1000;":
                "const MSE_DUREE_APRES_LECTURE_MS = 50;",
            "const MSE_DELAI_DECODAGE_FINAL_MS = 1000;":
                "const MSE_DELAI_DECODAGE_FINAL_MS = 5;",
        }
        for original, court in remplacements.items():
            if original not in bloc:
                raise AssertionError(f"constante MSE introuvable : {original}")
            bloc = bloc.replace(original, court)
        cls.bloc_mse = bloc

    def _executer(self, scenario: str) -> dict:
        script = r"""
// argv[1] est le chemin du script temporaire (plus -e : voir _executer),
// le scénario passé en argument de node se retrouve donc décalé en argv[2].
const scenario = process.argv[2];

class CibleEvenements {
  constructor() { this.listeners = Object.create(null); }
  addEventListener(type, callback, options) {
    if (!this.listeners[type]) this.listeners[type] = [];
    this.listeners[type].push({ callback, once: Boolean(options && options.once) });
  }
  removeEventListener(type, callback) {
    this.listeners[type] = (this.listeners[type] || [])
      .filter((entree) => entree.callback !== callback);
  }
  emettre(type) {
    for (const entree of [...(this.listeners[type] || [])]) {
      if (entree.once) this.removeEventListener(type, entree.callback);
      entree.callback({ type, target: this });
    }
  }
  nombreEcouteurs() {
    return Object.values(this.listeners).reduce((total, liste) => total + liste.length, 0);
  }
}

let video = null;
let hint = null;
let echecAffiche = null;
let arretAffiche = null;
let fetchCount = 0;
let readerCancelled = 0;
let bodyCancelled = 0;
const fetchSignals = [];
const mediaSources = [];
const sourceBuffers = [];
const urlsRevoquees = [];

class FausseVideo extends CibleEvenements {
  constructor() { super(); this.src = ""; this.playCount = 0; }
  play() { this.playCount += 1; return Promise.resolve(); }
}

class FauxSourceBuffer extends CibleEvenements {
  constructor() {
    super();
    this.mode = "segments";
    this.updating = false;
    this.aborted = false;
    sourceBuffers.push(this);
  }
  appendBuffer(_value) {
    this.updating = true;
    if (scenario === "pending_update") return;
    setTimeout(() => {
      this.updating = false;
      if (scenario === "decoded") video.emettre("loadeddata");
      this.emettre("updateend");
    }, 0);
  }
  abort() { this.aborted = true; this.updating = false; }
}

class FausseMediaSource extends CibleEvenements {
  constructor() {
    super();
    this.readyState = "closed";
    mediaSources.push(this);
    if (scenario !== "watch_timeout") {
      setTimeout(() => {
        this.readyState = "open";
        this.emettre("sourceopen");
      }, 0);
    }
  }
  addSourceBuffer(_mime) { return new FauxSourceBuffer(); }
  endOfStream() { this.readyState = "ended"; }
}
FausseMediaSource.isTypeSupported = () => true;

const box = {
  _html: "",
  set innerHTML(value) { this._html = value; hint = null; },
  get innerHTML() { return this._html; },
  querySelector(selecteur) { return selecteur === "video" ? video : null; },
  insertAdjacentHTML(_position, _html) {
    hint = {
      textContent: "",
      removed: false,
      remove() { this.removed = true; },
    };
  },
};

globalThis.window = {};
globalThis.MediaSource = FausseMediaSource;
globalThis.URL = {
  createObjectURL: (_objet) => `blob:test-${mediaSources.length}`,
  revokeObjectURL: (url) => urlsRevoquees.push(url),
};
globalThis.fetch = (url, options = {}) => {
  fetchCount += 1;
  fetchSignals.push(options.signal || null);
  if (scenario === "pending_fetch") return new Promise(() => {});
  if (url === "/api/live-error") {
    return Promise.resolve({json: async () => ({})});
  }
  if (scenario === "status_409") {
    return Promise.resolve({
      ok: false,
      status: 409,
      headers: {get: () => null},
      body: {cancel: () => { bodyCancelled += 1; return Promise.resolve(); }},
    });
  }
  let lecture = 0;
  const reader = {
    read: () => {
      if (scenario === "pending_read") return new Promise(() => {});
      lecture += 1;
      return Promise.resolve(lecture === 1
        ? {done: false, value: new Uint8Array([0, 1, 2, 3])}
        : {done: true});
    },
    cancel: () => { readerCancelled += 1; return Promise.resolve(); },
  };
  return Promise.resolve({
    ok: true,
    status: 200,
    headers: {get: (nom) => nom === "X-Codec" ? "avc1.42E01E" : null},
    body: {
      getReader: () => reader,
      cancel: () => { bodyCancelled += 1; return Promise.resolve(); },
    },
  });
};

const cssId = (name) => name;
const h = (value) => String(value);
const textes = {
  "watch.waking.mse": "Waking",
  "watch.reconnecting": "Reconnecting",
  "watch.noimage": "No image",
  "watch.refused.retry": "Busy",
};
const t = (cle) => textes[cle] || cle;
const tf = (cle, valeurs) => `${cle}:${valeurs.code || valeurs.codec || ""}`;
const $ = (id) => id.startsWith("live-") ? box
  : id.startsWith("hint-") && hint && !hint.removed ? hint : null;
const lireJSON = (reponse) => reponse.json();
const expandBtn = () => "";
const recordBtn = () => "";
const repos = () => "repos";
// Best-effort côté vrai code (direct.log seulement, cf. serve_app.js) : un
// no-op suffit ici, aucun test n'a besoin d'observer cet appel.
const signalerDirect = () => {};
function failWatch(_name, message) { echecAffiche = message; }
function stopWatch(name) {
  arretAffiche = name;
  const controller = MSE_ABORT[name];
  if (controller) { controller.abort(); delete MSE_ABORT[name]; }
}
""" + self.bloc_mse + r"""

(async () => {
  video = new FausseVideo();
  const debut = performance.now();
  if (scenario === "watch_timeout") {
    await watchMse("Cam");
    process.stdout.write(JSON.stringify({
      elapsed: performance.now() - debut,
      echecAffiche,
      arretAffiche,
      actif: Object.prototype.hasOwnProperty.call(MSE_ABORT, "Cam"),
      fetchCount,
      mediaSourceCount: mediaSources.length,
      revokeCount: urlsRevoquees.length,
      hintText: hint && hint.textContent,
    }));
    return;
  }

  hint = {
    textContent: "Waking",
    removed: false,
    remove() { this.removed = true; },
  };
  const parent = new AbortController();
  let valeur = null;
  let error = null;
  try {
    valeur = await connecterMse(
      "Cam", video, parent.signal, "Reconnecting", performance.now()
    );
  } catch (cause) {
    error = cause;
  }
  process.stdout.write(JSON.stringify({
    elapsed: performance.now() - debut,
    valeur,
    errorName: error && error.name,
    errorMessage: error && error.message,
    mseLecture: Boolean(error && error.mseLecture),
    status: error && error.status,
    fetchCount,
    fetchSignalAborted: Boolean(fetchSignals[0] && fetchSignals[0].aborted),
    readerCancelled,
    bodyCancelled,
    sourceBufferAborted: Boolean(sourceBuffers[0] && sourceBuffers[0].aborted),
    revokeCount: urlsRevoquees.length,
    hintText: hint && hint.textContent,
    hintRemoved: Boolean(hint && hint.removed),
    videoListeners: video.nombreEcouteurs(),
    mediaSourceListeners: mediaSources[0] && mediaSources[0].nombreEcouteurs(),
    metricType: typeof window.__mseMetric,
  }));
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exitCode = 1;
});
"""
        # Écrit sur disque plutôt que « node -e script » : bloc_mse suit la
        # taille de serve_app.js, et l'ajout du bouton d'enregistrement du
        # direct a fait dépasser à ce script inline la limite de ligne de
        # commande de Windows (WinError 206, constaté le 2026-09-05).
        fichier = tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", delete=False, encoding="utf-8")
        try:
            fichier.write(script)
            fichier.close()
            resultat = subprocess.run(
                [self.node, fichier.name, scenario],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
        finally:
            os.unlink(fichier.name)
        return json.loads(resultat.stdout)

    def test_sourceopen_absent_est_interrompu_par_le_budget_global(self):
        resultat = self._executer("watch_timeout")
        self.assertLess(resultat["elapsed"], 1000)
        self.assertEqual(resultat["echecAffiche"], "No image")
        self.assertFalse(resultat["actif"])
        self.assertEqual(resultat["fetchCount"], 0)
        self.assertGreaterEqual(resultat["mediaSourceCount"], 2)
        self.assertEqual(resultat["revokeCount"], resultat["mediaSourceCount"])
        self.assertEqual(resultat["hintText"], "Reconnecting")

    def test_segment_initial_sans_image_decodee_reste_un_echec(self):
        resultat = self._executer("init_only")
        self.assertIsNone(resultat["valeur"])
        self.assertEqual(resultat["errorMessage"], "No image")
        self.assertFalse(resultat["mseLecture"])
        self.assertEqual(resultat["readerCancelled"], 1)
        self.assertTrue(resultat["fetchSignalAborted"])
        self.assertEqual(resultat["revokeCount"], 1)
        self.assertEqual(resultat["hintText"], "Reconnecting")
        self.assertFalse(resultat["hintRemoved"])

    def test_loadeddata_seul_confirme_une_vraie_lecture(self):
        resultat = self._executer("decoded")
        self.assertTrue(resultat["valeur"])
        self.assertIsNone(resultat["errorMessage"])
        self.assertTrue(resultat["hintRemoved"])
        self.assertEqual(resultat["metricType"], "number")
        self.assertEqual(resultat["readerCancelled"], 1)
        self.assertTrue(resultat["fetchSignalAborted"])
        self.assertEqual(resultat["videoListeners"], 0)
        self.assertEqual(resultat["mediaSourceListeners"], 0)

    def test_fetch_bloque_est_abandonne_par_le_timeout_de_tentative(self):
        resultat = self._executer("pending_fetch")
        self.assertLess(resultat["elapsed"], 1000)
        self.assertEqual(resultat["errorMessage"], "No image")
        self.assertEqual(resultat["fetchCount"], 1)
        self.assertTrue(resultat["fetchSignalAborted"])
        self.assertEqual(resultat["revokeCount"], 1)

    def test_reader_bloque_est_annule_par_le_timeout_de_tentative(self):
        resultat = self._executer("pending_read")
        self.assertLess(resultat["elapsed"], 1000)
        self.assertEqual(resultat["errorMessage"], "No image")
        self.assertEqual(resultat["readerCancelled"], 1)
        self.assertTrue(resultat["fetchSignalAborted"])

    def test_updateend_bloque_annule_reader_et_sourcebuffer(self):
        resultat = self._executer("pending_update")
        self.assertLess(resultat["elapsed"], 1000)
        self.assertEqual(resultat["errorMessage"], "No image")
        self.assertEqual(resultat["readerCancelled"], 1)
        self.assertTrue(resultat["sourceBufferAborted"])
        self.assertTrue(resultat["fetchSignalAborted"])

    def test_409_garde_son_statut_et_annule_le_corps(self):
        resultat = self._executer("status_409")
        self.assertEqual(resultat["errorMessage"], "Busy")
        self.assertEqual(resultat["status"], 409)
        self.assertEqual(resultat["bodyCancelled"], 1)
        self.assertTrue(resultat["fetchSignalAborted"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
