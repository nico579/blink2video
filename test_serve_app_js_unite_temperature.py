"""Issue #146 : la température en Celsius ou en Fahrenheit, au choix de chaque
navigateur. Sans choix mémorisé, l'unité du pays du navigateur ; en Fahrenheit,
la mesure de Blink telle quelle, sans dériver par un aller-retour en Celsius.
Le vrai code de serve_app.js, exécuté par node.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

RACINE = Path(__file__).parent
SOURCE = (RACINE / "serve_app.js").read_text(encoding="utf-8")


def extraire(motif: str) -> str:
    trouve = re.search(motif, SOURCE, re.DOTALL | re.MULTILINE)
    if trouve is None:
        raise AssertionError(f"introuvable dans serve_app.js : {motif}")
    return trouve.group(0)


class UniteDeTemperature(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node introuvable")
        cls.code = "\n".join([
            extraire(r"^const CLE_UNITE_TEMPERATURE = .*?^// Fin des préférences d'unité\."),
            extraire(r'^function degres\(valeur, locale\) \{.*?^\}'),
            extraire(r'^function texteTemperature\(c, locale\) \{.*?^\}'),
        ])

    def executer(self, expression: str, memorise=None, langues=("fr-FR",)):
        script = (
            f"const _stock = {json.dumps({} if memorise is None else {'blink2video.uniteTemperature': memorise})};\n"
            "const localStorage = { getItem: (k) => (k in _stock ? _stock[k] : null) };\n"
            f"const navigator = {{ languages: {json.dumps(list(langues))} }};\n"
            + self.code + "\n"
            f"console.log(JSON.stringify({expression}));\n")
        resultat = subprocess.run([self.node, "-"], input=script, capture_output=True,
                                  text=True, encoding="utf-8", check=True)
        return json.loads(resultat.stdout)

    def test_l_unite_par_defaut_suit_le_pays_du_navigateur(self):
        # La règle de blink2temp (web.py et Android) : 22 territoires en Fahrenheit,
        # d'après le pays de la langue ; sans pays, Celsius.
        for langues, attendu in ((("en-US",), "F"), (("es-US", "en"), "F"), (("en-PR",), "F"),
                                 (("en",), "C"), (("en-GB",), "C"), (("fr-FR",), "C"), (("fr",), "C"),
                                 (("en-CA",), "C"), (("de",), "C"), ((), "C"), (("pas une langue",), "C")):
            with self.subTest(langues=langues):
                self.assertEqual(self.executer("uniteTemperature", langues=langues), attendu)

    def test_le_choix_memorise_l_emporte_et_une_valeur_abimee_est_ignoree(self):
        self.assertEqual(self.executer("uniteTemperature", memorise="F", langues=("fr-FR",)), "F")
        self.assertEqual(self.executer("uniteTemperature", memorise="C", langues=("en-US",)), "C")
        self.assertEqual(self.executer("uniteTemperature", memorise="K", langues=("fr-FR",)), "C")

    def test_en_fahrenheit_la_mesure_de_blink_telle_quelle(self):
        # 71 °F donnent 21,7 °C ; reconvertis, ils deviendraient 71,1 °F.
        camera = {"temperature": 21.7, "temperature_f": 71.0}
        self.assertEqual(self.executer(f"texteTemperature({json.dumps(camera)}, 'en-US')", memorise="F"),
                         "71.0 °F")
        self.assertEqual(self.executer(f"texteTemperature({json.dumps(camera)}, 'fr')", memorise="C"),
                         "21,7 °C")

    def test_un_releve_sans_temperature_f_se_reconvertit(self):
        # Un relevé gardé d'avant la 0.29.1 n'a que le Celsius.
        self.assertEqual(self.executer("texteTemperature({temperature: 20}, 'en-US')", memorise="F"), "68.0 °F")


class StructureDeLaPage(unittest.TestCase):
    def test_le_choix_est_dans_les_reglages_et_se_traduit(self):
        html = (RACINE / "serve.py").read_text(encoding="utf-8")
        self.assertEqual(html.count('id="uniteTemperature"'), 1)
        for cle in ("reglages.uniteTemperature", "reglages.uniteTemperature.C", "reglages.uniteTemperature.F"):
            with self.subTest(cle=cle):
                self.assertIn(f'data-i18n="{cle}"', html)
                self.assertEqual(len(re.findall(rf'"{re.escape(cle)}":', SOURCE)), 2,
                                 "une fois en français, une fois en anglais")

    def test_la_carte_n_ecrit_plus_les_degres_celsius_en_dur(self):
        self.assertNotIn("degres(c.temperature)} °C`", SOURCE)
        self.assertIn("num(c.temperature) ? texteTemperature(c) : null", SOURCE)


if __name__ == "__main__":
    unittest.main()
