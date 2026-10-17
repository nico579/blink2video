"""Le compteur décrit la détection configurée, pas l'armement du système."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


class TestsDetectionCount(unittest.TestCase):
    def test_detection_count_is_explicit_in_both_languages(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node introuvable")
        source = Path(__file__).with_name("serve_app.js").read_text(encoding="utf-8")
        patterns = [r"^const I18N = \{.*?^\};", r"^function t\(k\) \{[^\n]*\}$",
                    r"^function tf\(k, v\) \{.*?^\}", r"^function renderLive\(\) \{.*?^\}"]
        code = "\n".join(re.search(p, source, re.M | re.S).group(0) for p in patterns)
        for lang, expected in (("en", "3 camera(s) · 2 with detection on"),
                               ("fr", "3 caméra(s) · 2 avec détection active")):
            for system_armed in (False, True):
                with self.subTest(lang=lang, system_armed=system_armed):
                    systems = [{"name": "Test", "key": "test", "armed": system_armed,
                                "cameras": [{"armed": True}, {"armed": True}, {"armed": False}]}]
                    script = ("let _lang = " + json.dumps(lang) + "; let system = {systems: "
                              + json.dumps(systems) + "}; let rafraichirVignettes = false; "
                              "const elements = {count: {}, list: {}}; const $ = k => elements[k]; "
                              "const nomsDirectsActifs = () => []; const ronde = {boite: null}; const h = v => v; "
                              "const cameraCard = () => ''; const estMasquee = () => false; const comparerNoms = (a, b) => String(a).localeCompare(String(b));\n" + code +
                              "\nrenderLive(); console.log(JSON.stringify(elements.count.textContent));")
                    result = subprocess.run([node, "-"], input=script, capture_output=True,
                                            text=True, encoding="utf-8", check=True)
                    self.assertEqual(json.loads(result.stdout), expected)
