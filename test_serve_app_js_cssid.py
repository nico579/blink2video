r"""cssId() (serve_app.js) construisait l'identifiant DOM du bloc « direct »
de chaque caméra (id="live-${cssId(c.key)}", voir cameraCard()). Un double
antislash dans le regex (/[^\\w-]/g au lieu de /[^\w-]/g) faisait de la
classe de caractères « backslash, w ou tiret » plutôt que « alphanumérique
ou tiret » : dans un nom/clé réel, quasi tous les caractères tombaient hors
de cette classe et étaient donc remplacés, réduisant deux caméras
différentes au même identifiant. Conséquence : cliquer sur « voir en
direct » pour la deuxième caméra pouvait afficher son flux dans la carte
de la première (getElementById renvoie le premier élément qui porte l'id
dupliqué).

Exécute le VRAI code via Node (déjà présent sur les postes de développement
et les runners GitHub Actions), pas une réimplémentation Python. Sauté si
node est introuvable."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


class TestsCssId(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node introuvable")
        source = (Path(__file__).parent / "serve_app.js").read_text(encoding="utf-8")
        correspondance = re.search(
            r"const cssId = \(name\) => .*?;", source,
        )
        if correspondance is None:
            raise AssertionError("fonction cssId() introuvable dans serve_app.js")
        cls.fonction_cssid = correspondance.group(0)

    def _cssId(self, *noms: str) -> list:
        script = (
            self.fonction_cssid
            + "\nconst entrees = JSON.parse(process.argv[1]);"
            + "\nprocess.stdout.write(JSON.stringify(entrees.map(cssId)));"
        )
        resultat = subprocess.run(
            [self.node, "-e", script, json.dumps(noms)],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return json.loads(resultat.stdout)

    def test_deux_cameras_distinctes_gardent_des_ids_distincts(self):
        jardin, garage = self._cssId("Jardin", "Garage")
        self.assertNotEqual(jardin, garage)

    def test_une_cle_camera_v2_nest_pas_reduite_aux_seuls_tirets(self):
        (cle,) = self._cssId("camera-v2-a1b2c3d4e5f6")
        self.assertGreater(len(cle.strip("_")), 0)

    def test_alphanumerique_et_tiret_traversent_sans_changement(self):
        (nom,) = self._cssId("Jardin")
        self.assertEqual(nom, "Jardin")


if __name__ == "__main__":
    unittest.main(verbosity=2)
