"""Bug rapporté le 2026-09-12 (côté client) : rechargerEnArrierePlan() (appelé
sur nouveau clip détecté ou fin de travail de fond, cf. heuresDePassage() et
montrerTravail()) reconstruisait #list sans condition, détruisant toute
<video> de clip en cours de lecture - perçu comme un rafraîchissement
intempestif en plein visionnage. renderLive() a déjà ce garde-fou pour un
direct actif (bug identique constaté le 2026-08-27, voir son commentaire) ;
il manquait pour les clips et vidéos assemblées, qui partagent le même
conteneur #list.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


class TestsRechargementAutoPendantLecture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node introuvable")
        source = (Path(__file__).parent / "serve_app.js").read_text(encoding="utf-8")
        variable = re.search(r"^let dernierRechargementAuto = 0;$", source, re.MULTILINE)
        fonction = re.search(
            r"^function rechargerEnArrierePlan\(.*?^\}", source, re.DOTALL | re.MULTILINE,
        )
        if variable is None or fonction is None:
            raise AssertionError("rechargerEnArrierePlan() ou son état introuvable")
        cls.bloc = f"{variable.group(0)}\n{fonction.group(0)}"

    def _executer(self, pauses: list[bool]) -> dict:
        script = f"""
{self.bloc}

let chargements = 0;
function calculerSelection() {{ return {{ exclure: [], inclure: [], supprimer: [] }}; }}
function load() {{ chargements += 1; }}

const videos = {json.dumps(pauses)}.map((paused) => ({{ paused }}));
const liste = {{ querySelectorAll: (sel) => sel === "video" ? videos : [] }};
globalThis.$ = (id) => id === "list" ? liste : null;

rechargerEnArrierePlan();

process.stdout.write(JSON.stringify({{ chargements }}));
"""
        resultat = subprocess.run(
            [self.node, "-e", script], capture_output=True, text=True, timeout=10,
        )
        if resultat.returncode != 0:
            raise AssertionError(resultat.stderr)
        return json.loads(resultat.stdout)

    def test_clip_en_lecture_reporte_le_rechargement(self):
        self.assertEqual(self._executer([False])["chargements"], 0)

    def test_aucune_video_en_lecture_autorise_le_rechargement(self):
        self.assertEqual(self._executer([True, True])["chargements"], 1)

    def test_une_seule_video_en_lecture_parmi_plusieurs_suffit_a_reporter(self):
        self.assertEqual(self._executer([True, False, True])["chargements"], 0)


if __name__ == "__main__":
    unittest.main()
