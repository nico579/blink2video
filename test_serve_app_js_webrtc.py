"""Tests navigateur WebRTC hors ligne sur les fonctions JavaScript livrées."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path


class TestsNavigateurWebRTC(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node indisponible")
        source = Path(__file__).with_name("serve_app.js").read_text(encoding="utf-8")
        cls.source = (
            source[source.index('window.addEventListener("pagehide"'):source.index("// Toute valeur")]
            + source[source.index("async function loadSystem("):source.index("// Un calcul lancé")]
            + source[source.index("function nomsDirectsActifs()"):source.index("function cameraCard(")]
            + source[source.index("function arreterSessionWebRTC("):source.index("// --- MSE/fMP4")]
            + source[source.index("const MSE_ABORT = {};"):source.index("async function watchMse(")]
            + source[source.index('$("view").onchange ='):source.index('// Seule cette ligne de texte')]
        )
        for avant, apres in {
            "const WEBRTC_DELAI_DEMARRAGE_MS = 130 * 1000;":
                "const WEBRTC_DELAI_DEMARRAGE_MS = 150;",
            "const WEBRTC_DELAI_PREMIERE_IMAGE_MS = 30 * 1000;":
                "const WEBRTC_DELAI_PREMIERE_IMAGE_MS = 30;",
            "const WEBRTC_DUREE_APRES_LECTURE_MS = 330 * 1000;":
                "const WEBRTC_DUREE_APRES_LECTURE_MS = 100;",
            "const WEBRTC_BUDGET_TOTAL_MS = 10 * 60 * 1000;":
                "const WEBRTC_BUDGET_TOTAL_MS = 400;",
            "const WEBRTC_DELAI_MODULE_OCCUPE_MS = 10000;":
                "const WEBRTC_DELAI_MODULE_OCCUPE_MS = 3;",
            "const WEBRTC_DELAI_RECONNEXION_MS = 3000;":
                "const WEBRTC_DELAI_RECONNEXION_MS = 3;",
        }.items():
            if avant not in cls.source:
                raise AssertionError(avant)
            cls.source = cls.source.replace(avant, apres)

    def scenario(self, scenario):
        script = r'''
const scenario = process.argv[2];
const assert = require('node:assert/strict');
const crypto = require('node:crypto').webcrypto;
const pauses = ms => new Promise(resolve => setTimeout(resolve, ms));
const window = {events: {}, addEventListener(name, fn) { this.events[name] = fn; }};
const navigator = {sendBeacon(_url, body) { beacons.push(body); return true; }};
const BLINK_TOKEN = 'test-token';
const beacons = [], stops = [], offers = [], peers = [], pending = [], failures = [];
const t = key => key;
const tf = key => key;
const h = String;
const cssId = String;
const cameraSurBatterie = () => false;
const recordBtn = () => '';
const expandBtn = () => '';
const signalerDirect = () => {};
const failWatch = (name, message) => failures.push({name, message});
const repos = () => 'repos';
const lireJSON = r => r.json();
let system = {webrtc: true, systems: []};
class Video extends EventTarget {
  constructor() { super(); this.srcObject = null; this.readyState = 0; this.videoWidth = 0; }
  play() { return Promise.resolve(); }
}
const video = new Video();
const box = {innerHTML: 'original', isConnected: true, querySelector: () => video,
  insertAdjacentHTML() {}};
const list = {innerHTML: 'original'};
const view = {value: 'live'};
let loads = 0;
const load = () => loads++;
const render = () => {};
const hint = {isConnected: true, textContent: '', remove() { this.isConnected = false; }};
const $ = id => id === 'view' ? view : id === 'list' ? list : id === 'count' ? {} : id.startsWith('hint-') ? hint : box;
function fetch(url, options = {}) {
  if (url === '/api/system') return Promise.resolve({json: async () => ({webrtc:true, systems:[]})});
  if (url === '/api/arreter-direct') {
    stops.push(JSON.parse(options.body).session_id);
    return Promise.resolve({ok: true});
  }
  if (url === '/api/attente-module') {
    if (scenario === 'race' || scenario === 'pending_stop') {
      return new Promise(resolve => pending.push(resolve));
    }
    return Promise.resolve({json: async () => ({libre: true})});
  }
  const offer = JSON.parse(options.body);
  offers.push(offer);
  if (scenario === 'fetch_timeout' || scenario === 'abort_offer') return new Promise(() => {});
  if (scenario === 'retry_then_ended' && offers.length === 1) {
    return Promise.resolve({ok: false, status: 503, json: async () => ({error: 'first attempt failed'})});
  }
  if (scenario === 'busy' || scenario === 'busy_retry') {
    return Promise.resolve({ok: false, status: 409, json: async () => ({error: 'busy'})});
  }
  return Promise.resolve({ok: true, json: async () => ({
    sdp: 'answer', type: 'answer', session_id: offer.session_id,
  })});
}
class RTCPeerConnection extends EventTarget {
  constructor() { super(); this.connectionState = 'new'; this.iceConnectionState = 'new'; this.closed = 0; peers.push(this); }
  addTransceiver() {}
  async createOffer() { return {sdp: 'v=0', type: 'offer'}; }
  async setLocalDescription(offer) { this.localDescription = offer; }
  async setRemoteDescription() {
    this.connectionState = 'connected';
    const track = new EventTarget();
    this.ontrack({track, streams: [{}]});
    if (scenario === 'decoded' || scenario === 'ended' || scenario === 'retry_then_ended') {
      video.readyState = 2; video.videoWidth = 1920;
      video.dispatchEvent(new Event('loadeddata'));
      if (scenario !== 'decoded') setTimeout(() => track.dispatchEvent(new Event('ended')), 15);
    }
    if (scenario === 'ice_failed') {
      this.connectionState = 'failed';
      this.dispatchEvent(new Event('connectionstatechange'));
    }
  }
  close() { this.closed++; this.connectionState = 'closed'; }
}
''' + self.source + r'''
(async () => {
  const result = {};
  if (scenario === 'render') {
    WEBRTC_PC.Cam = {};
    renderLive();
    assert.equal(list.innerHTML, 'original');
    delete WEBRTC_PC.Cam;
    LIVE_PENDING.Cam = new AbortController();
    renderLive();
    assert.equal(list.innerHTML, 'original');
  } else if (scenario === 'system_refresh') {
    WEBRTC_PC.Cam = {};
    await loadSystem(true);
    assert.equal(list.innerHTML, 'original');
  } else if (scenario === 'leave_view') {
    WEBRTC_SESSION.Cam = 'owned-session';
    WEBRTC_PC.Cam = new RTCPeerConnection();
    view.value = 'clips';
    view.onchange();
    assert.equal(loads, 1);
    assert.deepEqual(stops, ['owned-session']);
    assert.equal(Object.keys(WEBRTC_PC).length, 0);
  } else if (scenario === 'pagehide') {
    window.events.pagehide();
    assert.equal(beacons.length, 0);
    WEBRTC_SESSION.Cam = 'owned-session';
    WEBRTC_PC.Cam = new RTCPeerConnection();
    window.events.pagehide();
    assert.equal(beacons.length, 1);
    assert.equal(JSON.parse(await beacons[0].text()).session_id, 'owned-session');
    assert.equal(Object.keys(WEBRTC_PC).length, 0);
  } else if (scenario === 'race' || scenario === 'pending_stop') {
    const starts = [];
    watchWebRTC = async name => { starts.push(name); };
    const a = watchLive('A');
    await pauses(0);
    assert.ok(LIVE_PENDING.A);
    assert.ok(box.innerHTML.includes('stop-live'));
    if (scenario === 'pending_stop') {
      stopWatch('A');
      await a;
      assert.deepEqual(starts, []);
    } else {
      const b = watchLive('B');
      await pauses(0);
      pending[1]({json: async () => ({libre: true})});
      await b;
      pending[0]({json: async () => ({libre: true})});
      await a;
      assert.deepEqual(starts, ['B']);
    }
    assert.equal(Object.keys(LIVE_PENDING).length, 0);
  } else if (scenario === 'retry_then_ended') {
    await watchWebRTC('Cam');
    assert.equal(offers.length, 2);
    assert.equal(Object.keys(WEBRTC_ABORT).length, 0);
    assert.equal(failures.length, 0);
    assert.equal(box.innerHTML, 'repos');
  } else if (scenario === 'busy_retry') {
    await watchWebRTC('Cam');
    assert.ok(offers.length > WEBRTC_MAX_ECHECS);
    assert.equal(Object.keys(WEBRTC_ABORT).length, 0);
    assert.equal(failures.length, 1);
  } else {
    const controller = new AbortController();
    let displayed = 0;
    const started = performance.now();
    const promise = tenterWebRTC('Cam', video, controller.signal, 1, () => displayed++);
    if (scenario === 'abort_offer') setTimeout(() => controller.abort(), 10);
    let error;
    try { result.value = await promise; } catch (e) { error = e; }
    result.elapsed = performance.now() - started;
    if (scenario === 'decoded' || scenario === 'ended') {
      assert.equal(displayed, 1);
      assert.equal(result.value, true);
      assert.ok(result.elapsed >= (scenario === 'decoded' ? 80 : 10));
    } else {
      assert.ok(error);
      assert.equal(displayed, 0);
      if (scenario === 'busy') assert.equal(error.status, 409);
      if (scenario === 'abort_offer') assert.equal(error.name, 'AbortError');
      if (scenario === 'no_image') assert.ok(result.elapsed >= 20);
      if (scenario === 'fetch_timeout') assert.ok(result.elapsed >= 100);
    }
    assert.equal(Object.keys(WEBRTC_PC).length, 0);
    assert.equal(Object.keys(WEBRTC_SESSION).length, 0);
    assert.equal(peers[0].closed, 1);
    assert.equal(stops.length, 1);
    assert.equal(stops[0], offers[0].session_id);
  }
  process.stdout.write(JSON.stringify({ok:true, ...result}));
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
'''
        result = subprocess.run([self.node, "-", scenario], input=script,
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_grille_preserve_video_et_demande_en_attente(self):
        self.scenario("render")

    def test_pagehide_ne_coupe_que_les_sessions_de_son_onglet(self):
        self.scenario("pagehide")

    def test_actualisation_etat_preserve_video_active(self):
        self.scenario("system_refresh")

    def test_quitter_direct_ferme_sa_session_avant_rendu_clips(self):
        self.scenario("leave_view")

    def test_dernier_clic_gagne_meme_si_reponses_inversees(self):
        self.scenario("race")

    def test_arret_possible_pendant_attente_module(self):
        self.scenario("pending_stop")

    def test_sdp_sans_image_expire_et_ferme_session(self):
        self.scenario("no_image")

    def test_fetch_bloque_est_interrompu_par_budget(self):
        self.scenario("fetch_timeout")

    def test_annulation_offre_en_cours_envoie_arret_cible(self):
        self.scenario("abort_offer")

    def test_echec_ice_initial_ne_compte_pas_comme_lecture(self):
        self.scenario("ice_failed")

    def test_lecture_confirmee_puis_nettoyage_a_la_fin(self):
        self.scenario("decoded")

    def test_fin_piste_ferme_session_sans_attendre_plafond(self):
        self.scenario("ended")

    def test_statut_busy_est_conserve(self):
        self.scenario("busy")

    def test_busy_n_epuise_pas_les_essais_camera(self):
        self.scenario("busy_retry")

    def test_reprise_reussie_efface_erreur_a_la_fin_normale(self):
        self.scenario("retry_then_ended")


if __name__ == "__main__":
    unittest.main()
