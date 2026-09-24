"""Non-régression : md.save_json() appelé en parallèle sur un même fichier.

serve.py tourne sur un ThreadingHTTPServer, et la page charge /api/clips et
/api/videos en même temps (Promise.all dans load(), serve_app.js). Dès qu'il
existe au moins un enregistrement du direct et une vidéo assemblée, les deux
routes réécrivent ASSEMBLED_DURATIONS. Avec un temporaire au nom fixe
(``path.with_suffix(".tmp")``), le second ``replace`` trouvait le fichier déjà
consommé par le premier : FileNotFoundError, donc une requête en échec au
chargement de la page (reproduit 197 fois sur 600 écritures, audit du
2026-09-24)."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path

os.environ.setdefault("BLINK_BOOTSTRAP", "none")

import merge_daily as md  # noqa: E402


class TestsSaveJsonConcurrent(unittest.TestCase):
    def setUp(self):
        dossier = tempfile.TemporaryDirectory(prefix="blink-save-json-")
        self.addCleanup(dossier.cleanup)
        self.dossier = Path(dossier.name)
        self.cible = self.dossier / "assembled_durations.json"

    def test_ecritures_paralleles_sans_exception_ni_residu(self):
        valeur = {f"daily/cam/{i}.mp4": {"duration": 12.5, "empreinte": [123, 1.0]}
                  for i in range(300)}
        erreurs = []
        depart = threading.Barrier(4)

        def ecrire():
            depart.wait()
            for _ in range(150):
                try:
                    md.save_json(self.cible, valeur)
                except Exception as erreur:  # noqa: BLE001 - tout échec compte
                    erreurs.append(erreur)

        fils = [threading.Thread(target=ecrire) for _ in range(4)]
        for fil in fils:
            fil.start()
        for fil in fils:
            fil.join()

        self.assertEqual(erreurs, [])
        self.assertEqual(md.load_json(self.cible, None), valeur)
        # Aucun temporaire laissé derrière, quel que soit son nom.
        self.assertEqual(sorted(p.name for p in self.dossier.iterdir()),
                         ["assembled_durations.json"])

    def test_cree_le_dossier_parent(self):
        cible = self.dossier / "sous" / "dossier" / "etat.json"
        md.save_json(cible, {"a": 1})
        self.assertEqual(md.load_json(cible, {}), {"a": 1})


if __name__ == "__main__":
    unittest.main()
