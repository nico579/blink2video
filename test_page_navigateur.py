"""La page de blink2video dans un vrai navigateur.

Les tests test_serve_app_js_*.py exécutent des tranches de serve_app.js sous
Node, avec un DOM et une horloge simulés ; ils ne voient ni la page entière, ni
la mise en page, ni les vrais événements. Ici, le serveur de page (serve.py) est
lancé depuis les sources, avec un BLINK_HOME jetable et sans compte Blink, et
Playwright (bibliothèque de Microsoft qui pilote un vrai Chromium) charge la
page : aucune erreur JavaScript ni réponse en échec, bascule FR / EN, dialogue
Réglages et ses onglets, changement de vue. Rien n'y contacte Blink : le
bouton Actualiser n'est jamais pressé.

Jumeau de test_page_navigateur.py de gpxsolar et de lidar2map. Sans
Playwright, le test est sauté ; la CI l'installe (job « navigateur »).

    python -m pip install playwright
    python -m playwright install chromium
    python test_page_navigateur.py
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

try:
    from playwright.sync_api import Error as ErreurPlaywright, expect, sync_playwright
except ImportError:
    sync_playwright = None

RACINE = Path(__file__).resolve().parent


def port_libre():
    with socket.socket() as sonde:
        sonde.bind(("127.0.0.1", 0))
        return sonde.getsockname()[1]


def repond(adresse):
    ouvreur = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with ouvreur.open(adresse, timeout=1) as reponse:
            return b"<title>blink2video</title>" in reponse.read()
    except OSError:
        return False


@unittest.skipIf(sync_playwright is None, "Playwright absent")
class PageBlink2video(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dossier = tempfile.TemporaryDirectory()
        cls.journal = open(Path(cls.dossier.name) / "serveur.log", "wb")
        port = port_libre()
        environnement = dict(os.environ, BLINK_HOME=cls.dossier.name, BLINK_BOOTSTRAP="none")
        cls.processus = subprocess.Popen(
            [sys.executable, str(RACINE / "serve.py"), "--port", str(port)],
            cwd=cls.dossier.name, env=environnement, stdout=cls.journal, stderr=subprocess.STDOUT)
        cls.adresse = f"http://127.0.0.1:{port}/"
        limite = time.monotonic() + 120
        while not repond(cls.adresse):
            if cls.processus.poll() is not None or time.monotonic() > limite:
                cls.arreter()
                raise AssertionError("blink2video n'a pas démarré : "
                                     + (Path(cls.dossier.name) / "serveur.log").read_text(errors="replace")[-2000:])
            time.sleep(.2)
        cls.playwright = sync_playwright().start()
        try:
            cls.navigateur = cls.playwright.chromium.launch(headless=True)
        except ErreurPlaywright as erreur:
            cls.playwright.stop()
            cls.arreter()
            raise unittest.SkipTest(f"Chromium absent : {erreur}")

    @classmethod
    def arreter(cls):
        if cls.processus.poll() is None:
            cls.processus.terminate()
            try:
                cls.processus.wait(15)
            except subprocess.TimeoutExpired:
                cls.processus.kill()
                cls.processus.wait()
        cls.journal.close()
        cls.dossier.cleanup()

    @classmethod
    def tearDownClass(cls):
        cls.navigateur.close()
        cls.playwright.stop()
        cls.arreter()

    def setUp(self):
        self.contexte = self.navigateur.new_context(locale="fr-FR", viewport={"width": 1280, "height": 800})
        self.page = self.contexte.new_page()
        self.erreurs = []
        self.page.on("pageerror", lambda erreur: self.erreurs.append(str(erreur)))
        self.page.on("console", lambda message: self.erreurs.append(message.text)
                     if message.type == "error" else None)
        self.page.on("response", lambda r: self.erreurs.append(f"HTTP {r.status} {r.url}")
                     if r.status >= 400 else None)
        self.page.goto(self.adresse)
        expect(self.page.locator("#refresh")).to_be_visible()

    def tearDown(self):
        self.contexte.close()
        self.assertEqual(self.erreurs, [])

    def test_la_page_se_charge_et_change_de_langue(self):
        expect(self.page.locator("#refresh")).to_have_text("↻ Actualiser")
        self.page.locator('header [data-lang-btn="en"]').click()
        expect(self.page.locator("html")).to_have_attribute("lang", "en")
        expect(self.page.locator("#refresh")).to_have_text("↻ Refresh")
        expect(self.page.locator("#reglagesButton")).to_have_text("⚙ Settings…")
        self.page.locator('header [data-lang-btn="fr"]').click()
        expect(self.page.locator("#refresh")).to_have_text("↻ Actualiser")

    def test_le_dialogue_reglages_et_ses_onglets(self):
        self.page.locator("#reglagesButton").click()
        reglages = self.page.locator("#reglages")
        expect(reglages).to_be_visible()
        # La case « Démarrer automatiquement » : affichée, jamais cochée ici,
        # puisqu'elle écrit dans le système.
        expect(self.page.locator("#autostart")).to_be_visible()
        expect(self.page.locator("#panelGeneral")).to_be_visible()
        self.page.keyboard.press("Escape")
        expect(reglages).to_be_hidden()

    def test_le_changement_de_vue(self):
        self.page.locator("#view").select_option("pictures")
        expect(self.page.locator("#view")).to_have_value("pictures")
        self.page.locator("#view").select_option("clips")
        expect(self.page.locator("#view")).to_have_value("clips")

    def test_la_ronde_des_directs(self):
        # Issue #40. Toujours sans Blink : /api/system et les vignettes sont
        # simulés, et watchLive() montre un flux tiré d'un canvas, qui émet de
        # vrais « playing ». Ce que Node ne voit pas est ici réel : les cases
        # déplacées dans le DOM, les clics, la mise en page. Le plein écran
        # dépend du navigateur et n'est pas exigé.
        systeme = {"webrtc": True, "passages": {}, "systems": [
            {"name": "Dehors", "key": "dehors", "armed": True, "cameras": [
                {"name": "jardin", "key": "k-jardin", "armed": True},
                {"name": "Terrasse", "key": "k-terrasse", "armed": False},
                {"name": "Garage", "key": "k-garage", "armed": True, "offline": True}]}]}
        self.page.route("**/api/system", lambda route: route.fulfill(json=systeme))
        self.page.route("**/camthumb/**", lambda route: route.fulfill(
            status=200, content_type="image/gif", body=GIF_1X1))
        self.page.evaluate(DIRECT_SIMULE)
        self.page.locator("#view").select_option("live")
        expect(self.page.locator("#rondeButton")).to_be_visible()
        expect(self.page.locator("#list .live")).to_have_count(3)

        self.page.locator("#rondeButton").click()
        expect(self.page.locator("#rondeCameras label")).to_have_count(3)
        self.page.locator("#rondeSecondes").fill("10")
        self.page.locator("#rondeDemarrer").click()
        expect(self.page.locator("#ronde")).to_be_visible()
        # Garage, hors ligne, est sauté.
        expect(self.page.locator("#rondeTitre")).to_have_text("jardin · 1/2")
        expect(self.page.locator("#rondeScene #live-k-jardin video")).to_have_count(1)
        self.page.wait_for_function("() => ronde.image")

        self.page.locator("#rondeSuivante").click()
        expect(self.page.locator("#rondeTitre")).to_have_text("Terrasse · 2/2")
        expect(self.page.locator("#list #live-k-jardin")).to_have_count(1)

        self.page.locator("#rondeArreter").click()
        expect(self.page.locator("#ronde")).to_be_hidden()
        expect(self.page.locator("#list .live")).to_have_count(3)
        expect(self.page.locator("#rondeScene > *")).to_have_count(0)

        # Échap arrête aussi, plein écran accordé ou non.
        self.page.locator("#rondeButton").click()
        self.page.locator("#rondeDemarrer").click()
        expect(self.page.locator("#rondeTitre")).to_have_text("jardin · 1/2")
        self.page.keyboard.press("Escape")
        expect(self.page.locator("#ronde")).to_be_hidden()
        expect(self.page.locator("#list .live")).to_have_count(3)


GIF_1X1 = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00"
           b"\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")

# Remplace le direct par un flux de canvas : aucune requête vers le serveur.
DIRECT_SIMULE = """() => {
  watchLive = (cle) => {
    const toile = document.createElement("canvas");
    toile.width = 320; toile.height = 180;
    const dessin = toile.getContext("2d");
    setInterval(() => { dessin.fillStyle = "#" + (Date.now() % 4096).toString(16).padStart(3, "0");
                        dessin.fillRect(0, 0, 320, 180); }, 100);
    const video = document.createElement("video");
    video.muted = true; video.autoplay = true; video.playsInline = true;
    video.srcObject = toile.captureStream(10);
    const boite = document.getElementById("live-" + cssId(cle));
    boite.replaceChildren(video);
    return new Promise(() => {});
  };
  stopWatch = (cle) => {
    const boite = document.getElementById("live-" + cssId(cle));
    if (boite) boite.innerHTML = repos(cle, t("watch.live"));
  };
}"""


if __name__ == "__main__":
    unittest.main()
