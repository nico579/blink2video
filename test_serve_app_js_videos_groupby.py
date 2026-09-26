"""Groupement par jour de la vue journalières (issue #14) : sans lui, voir
toutes les caméras d'une même journée oblige à rouvrir chaque caméra une
par une. label d'une journalière est déjà AAAA-MM-JJ (merge_daily.py,
f"{day}_{camera}.mp4"), le tri alphabétique EST le tri chronologique."""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path


class TestsGroupementVideosParJour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node introuvable")
        source = (Path(__file__).parent / "serve_app.js").read_text(encoding="utf-8")
        fragments = []
        for nom in ("duration", "dateLocale", "renderVideos", "videoCard"):
            correspondance = re.search(
                rf"^(?:async )?function {nom}\(.*?^\}}", source, re.DOTALL | re.MULTILINE,
            )
            if correspondance is None:
                raise AssertionError(f"fonction {nom}() introuvable")
            fragments.append(correspondance.group(0))
        cls.javascript = "\n".join(fragments)

    def _executer(self, videos_daily: list, group_by: str, camera_filtre: str = "") -> dict:
        script = f"""
const h = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({{
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}})[char]);
const avecJeton = (url) => url;
function t(cle) {{ return cle; }}
function tf(cle, valeurs) {{ return cle; }}

const boxes = {{
  camera: {{ value: {camera_filtre!r} }},
  groupBy: {{ value: {group_by!r} }},
  count: {{ set textContent(v) {{}} }},
  list: {{ innerHTML: "" }},
}};
function $(id) {{ return boxes[id]; }}

const videos = {{ daily: {videos_daily} }};

{self.javascript}

renderVideos("daily");
process.stdout.write(boxes.list.innerHTML);
"""
        resultat = subprocess.run(
            [self.node, "-e", script], capture_output=True, text=True, timeout=60,
        )
        if resultat.returncode != 0:
            self.fail(f"Node a échoué : {resultat.stderr}")
        return resultat.stdout

    def _video(self, camera: str, label: str, path: str = "x.mp4") -> dict:
        return {
            "kind": "daily", "camera": camera, "label": label,
            "path": path, "duration": 60,
        }

    def test_groupe_par_camera_est_le_comportement_par_defaut(self):
        html = self._executer(
            [self._video("Jardin", "2026-09-20"), self._video("Salon", "2026-09-20")],
            group_by="camera",
        )
        self.assertLess(html.index("<h2>Jardin</h2>"), html.index("<h2>Salon</h2>"))

    def test_groupe_par_jour_rassemble_toutes_les_cameras_d_un_meme_jour(self):
        html = self._executer(
            [self._video("Jardin", "2026-09-19"), self._video("Salon", "2026-09-20"),
             self._video("Jardin", "2026-09-20")],
            group_by="day",
        )
        # Deux groupes de jour seulement (pas un par camera : sans le
        # groupement, "Jardin" ferait un 3e groupe avec ses 2 videos).
        self.assertEqual(html.count("<h2>"), 2)
        # Le seul jour a avoir 2 cameras (le 20) doit en montrer 2 avant que
        # le second groupe (le 19, une seule camera) n'apparaisse : "Salon"
        # n'existe que dans le groupe du 20, jamais dans celui du 19.
        self.assertLess(html.index("Salon"), html.rindex("Jardin"))
        self.assertLess(html.index("Jardin"), html.index("Salon"))

    def test_groupe_par_jour_affiche_la_camera_sur_chaque_carte(self):
        html = self._executer([self._video("Jardin", "2026-09-20")], group_by="day")
        self.assertIn('<div class="time">Jardin</div>', html)
        # Le label (identique a la date deja en h2) ne doit pas etre repete.
        self.assertNotIn('<div class="time">2026-09-20</div>', html)

    def test_groupe_par_camera_affiche_le_label_sur_chaque_carte(self):
        html = self._executer([self._video("Jardin", "2026-09-20")], group_by="camera")
        self.assertIn('<div class="time">2026-09-20</div>', html)


if __name__ == "__main__":
    unittest.main()
