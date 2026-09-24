"""Non-régression : exclusions de directs demandées en même temps.

_appliquer_selection_directs() relit DIRECT_EXCLUSION, la modifie et la
réécrit. Sur le ThreadingHTTPServer, deux sélections simultanées (double-clic,
deux onglets) relisaient la même liste et la dernière écriture effaçait
l'autre : 39 exclusions perdues sur 40 lancées ensemble (2026-09-24)."""

import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock


_IMPORT_HOME = tempfile.TemporaryDirectory(prefix="blink-exclusion-import-")
with mock.patch.dict(os.environ, {"BLINK_BOOTSTRAP": "none",
                                  "BLINK_HOME": _IMPORT_HOME.name}):
    import serve


class TestsExclusionDirecteConcurrente(unittest.TestCase):
    def setUp(self):
        temporaire = tempfile.TemporaryDirectory(prefix="blink-exclusion-directe-")
        self.addCleanup(temporaire.cleanup)
        racine = Path(temporaire.name)
        self.paths = {"thumbs": racine / "thumbs", "direct": racine / "direct"}
        for chemin in self.paths.values():
            chemin.mkdir()
        self.handler = serve.Handler.__new__(serve.Handler)
        self.handler.paths = self.paths

    def test_selections_simultanees_ne_perdent_aucune_exclusion(self):
        nombre = 20
        depart = threading.Barrier(nombre)
        erreurs = []

        def exclure(i):
            depart.wait()
            try:
                self.handler._appliquer_selection_directs(
                    serve._SelectionVideos([f"cam/{i}.mp4"], [], []),
                    self.paths["direct"], {}, {})
            except Exception as erreur:  # noqa: BLE001 - tout échec compte
                erreurs.append(erreur)

        fils = [threading.Thread(target=exclure, args=(i,)) for i in range(nombre)]
        for fil in fils:
            fil.start()
        for fil in fils:
            fil.join()

        self.assertEqual(erreurs, [])
        self.assertEqual(serve._lire_exclusion_directe(self.paths),
                         {f"cam/{i}.mp4" for i in range(nombre)})


if __name__ == "__main__":
    unittest.main()
