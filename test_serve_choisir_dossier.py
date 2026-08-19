"""Non-régression de /api/choisir-dossier (AUDIT-2026-08-13.md, section
28.41) : sélecteur de dossier natif côté serveur, tkinter étant importé à
la demande pour ne jamais peser sur un environnement sans affichage (CI
Linux headless, qui n'emprunte jamais cette route)."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-choisir-dossier-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import serve  # noqa: E402 - bootstrap neutralisé avant import


def construire_faux_tkinter(chemin_retourne):
    """Simule tkinter.Tk()/filedialog.askdirectory() sans jamais ouvrir de
    vraie fenêtre : seule la valeur de retour d'askdirectory() compte ici,
    la route ne fait rien d'autre du module."""
    faux_tkinter = types.ModuleType("tkinter")

    class FauxTk:
        def withdraw(self):
            pass

        def attributes(self, *args, **kwargs):
            pass

        def destroy(self):
            pass

    faux_tkinter.Tk = FauxTk

    faux_filedialog = types.ModuleType("tkinter.filedialog")
    faux_filedialog.askdirectory = mock.Mock(return_value=chemin_retourne)
    faux_tkinter.filedialog = faux_filedialog
    return faux_tkinter, faux_filedialog


class TestChoisirDossier(unittest.TestCase):
    def construire_handler(self):
        handler = serve.Handler.__new__(serve.Handler)
        handler.paths = {}
        handler.timezone = ZoneInfo("UTC")
        handler.path = "/api/choisir-dossier"
        handler.headers = {}
        handler.rfile = io.BytesIO(b"")
        return handler

    def appeler(self, handler):
        reponses = []
        handler.send_json = lambda payload, code=200: reponses.append((code, payload))
        handler.do_GET()
        return reponses

    def test_renvoie_le_chemin_choisi(self):
        faux_tkinter, faux_filedialog = construire_faux_tkinter("D:/photos/blink")
        with mock.patch.dict(sys.modules,
                             {"tkinter": faux_tkinter, "tkinter.filedialog": faux_filedialog}):
            reponses = self.appeler(self.construire_handler())
        self.assertEqual(reponses, [(200, {"path": "D:/photos/blink"})])

    def test_annulation_renvoie_un_chemin_vide(self):
        # tkinter.filedialog.askdirectory() renvoie "" (tuple vide en
        # interne) quand l'utilisateur ferme la boîte sans choisir.
        faux_tkinter, faux_filedialog = construire_faux_tkinter("")
        with mock.patch.dict(sys.modules,
                             {"tkinter": faux_tkinter, "tkinter.filedialog": faux_filedialog}):
            reponses = self.appeler(self.construire_handler())
        self.assertEqual(reponses, [(200, {"path": ""})])

    def test_echec_du_selecteur_repond_en_erreur_plutot_que_de_planter(self):
        faux_tkinter = types.ModuleType("tkinter")

        class TkQuiEchoue:
            def __init__(self):
                raise RuntimeError("no display")

        faux_tkinter.Tk = TkQuiEchoue
        faux_filedialog = types.ModuleType("tkinter.filedialog")
        faux_filedialog.askdirectory = mock.Mock()
        faux_tkinter.filedialog = faux_filedialog
        with mock.patch.dict(sys.modules,
                             {"tkinter": faux_tkinter, "tkinter.filedialog": faux_filedialog}):
            reponses = self.appeler(self.construire_handler())
        self.assertEqual(len(reponses), 1)
        code, payload = reponses[0]
        self.assertEqual(code, 500)
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
