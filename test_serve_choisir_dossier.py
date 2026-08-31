"""Non-régression de /api/choisir-dossier (AUDIT-2026-08-13.md, section
28.41) : sélecteur de dossier natif côté serveur. PowerShell/WinForms sous
Windows (voir _choisir_dossier_windows, revue du 27/08 : tkinter n'est pas
garanti dans le bundle Windows 7, "Sélecteur de dossier indisponible"
constaté en conditions réelles) ; tkinter en repli ailleurs, importé à la
demande pour ne jamais peser sur un environnement sans affichage (CI Linux
headless, qui n'emprunte jamais cette route)."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8, édition Windows 7
    from backports.zoneinfo import ZoneInfo

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-choisir-dossier-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import runtime  # noqa: E402 - bootstrap neutralisé avant import
import serve  # noqa: E402


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
        # Host requis depuis 28.60 (protection CSRF/Origin sur do_GET) :
        # sans lui, ce handler construit à la main est rejeté en 403.
        handler.headers = {
            "Host": "127.0.0.1",
            "X-Blink-Token": serve.TOKEN,
        }
        handler.rfile = io.BytesIO(b"")
        return handler

    def appeler(self, handler):
        reponses = []
        handler.send_json = lambda payload, code=200: reponses.append((code, payload))
        handler.do_GET()
        return reponses

    # --- Windows : PowerShell/WinForms (_choisir_dossier_windows) ---

    def test_windows_renvoie_le_chemin_choisi(self):
        handler = self.construire_handler()
        with mock.patch.object(serve.os, "name", "nt"), \
             mock.patch.object(serve.runtime, "lire_dossier_stockage",
                               return_value="C:/depart"), \
             mock.patch.object(serve, "_choisir_dossier_windows",
                               return_value="D:/photos/blink") as appel:
            reponses = self.appeler(handler)
        self.assertEqual(reponses, [(200, {"path": "D:/photos/blink"})])
        appel.assert_called_once_with("C:/depart")

    def test_windows_annulation_renvoie_un_chemin_vide(self):
        handler = self.construire_handler()
        with mock.patch.object(serve.os, "name", "nt"), \
             mock.patch.object(serve.runtime, "lire_dossier_stockage",
                               return_value="C:/depart"), \
             mock.patch.object(serve, "_choisir_dossier_windows", return_value=""):
            reponses = self.appeler(handler)
        self.assertEqual(reponses, [(200, {"path": ""})])

    def test_windows_echec_du_selecteur_repond_en_erreur_plutot_que_de_planter(self):
        handler = self.construire_handler()
        with mock.patch.object(serve.os, "name", "nt"), \
             mock.patch.object(serve.runtime, "lire_dossier_stockage",
                               return_value="C:/depart"), \
             mock.patch.object(
                 serve, "_choisir_dossier_windows",
                 side_effect=RuntimeError("PowerShell indisponible"),
             ) as appel:
            reponses = self.appeler(handler)
        self.assertEqual(len(reponses), 1)
        code, payload = reponses[0]
        self.assertEqual(code, 500)
        self.assertEqual(payload["error"], "PowerShell indisponible")
        appel.assert_called_once_with("C:/depart")

    # --- Hors Windows : repli tkinter ---

    def test_repli_renvoie_le_chemin_choisi(self):
        # Handler construit AVANT le patch de os.name : os.name est un
        # attribut du module os réel (partagé), pas une copie propre à
        # serve - le patcher pendant la construction (ZoneInfo(), etc. y
        # touchent indirectement) fait planter pathlib sur ce système
        # réellement Windows, hors sujet ici.
        handler = self.construire_handler()
        faux_tkinter, faux_filedialog = construire_faux_tkinter("D:/photos/blink")
        with mock.patch.object(serve.os, "name", "posix"), \
             mock.patch.object(serve.runtime, "lire_dossier_stockage", return_value=""), \
             mock.patch.dict(sys.modules,
                             {"tkinter": faux_tkinter, "tkinter.filedialog": faux_filedialog}):
            reponses = self.appeler(handler)
        self.assertEqual(reponses, [(200, {"path": "D:/photos/blink"})])

    def test_repli_annulation_renvoie_un_chemin_vide(self):
        # tkinter.filedialog.askdirectory() renvoie "" (tuple vide en
        # interne) quand l'utilisateur ferme la boîte sans choisir.
        handler = self.construire_handler()
        faux_tkinter, faux_filedialog = construire_faux_tkinter("")
        with mock.patch.object(serve.os, "name", "posix"), \
             mock.patch.object(serve.runtime, "lire_dossier_stockage", return_value=""), \
             mock.patch.dict(sys.modules,
                             {"tkinter": faux_tkinter, "tkinter.filedialog": faux_filedialog}):
            reponses = self.appeler(handler)
        self.assertEqual(reponses, [(200, {"path": ""})])

    def test_repli_echec_du_selecteur_repond_en_erreur_plutot_que_de_planter(self):
        handler = self.construire_handler()
        faux_tkinter = types.ModuleType("tkinter")

        class TkQuiEchoue:
            def __init__(self):
                raise RuntimeError("no display")

        faux_tkinter.Tk = TkQuiEchoue
        faux_filedialog = types.ModuleType("tkinter.filedialog")
        faux_filedialog.askdirectory = mock.Mock()
        faux_tkinter.filedialog = faux_filedialog
        with mock.patch.object(serve.os, "name", "posix"), \
             mock.patch.object(serve.runtime, "lire_dossier_stockage", return_value=""), \
             mock.patch.dict(sys.modules,
                             {"tkinter": faux_tkinter, "tkinter.filedialog": faux_filedialog}):
            reponses = self.appeler(handler)
        self.assertEqual(len(reponses), 1)
        code, payload = reponses[0]
        self.assertEqual(code, 500)
        self.assertIn("error", payload)


class TestChoisirDossierWindows(unittest.TestCase):
    """_choisir_dossier_windows() elle-même, sans jamais lancer un vrai
    PowerShell : seule la construction de la commande et l'interprétation de
    sa sortie sont en jeu ici."""

    def test_chemin_choisi_est_lu_sur_stdout(self):
        resultat = mock.Mock(returncode=0, stdout="D:\\photos\\blink\r\n", stderr="")
        with mock.patch.object(runtime, "lancer", return_value=resultat) as appel:
            chemin = serve._choisir_dossier_windows("C:\\depart")
        self.assertEqual(chemin, "D:\\photos\\blink")
        commande = appel.call_args[0][0]
        self.assertEqual(commande[0], "powershell")

    def test_annulation_renvoie_une_chaine_vide(self):
        resultat = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(runtime, "lancer", return_value=resultat):
            self.assertEqual(serve._choisir_dossier_windows("C:\\depart"), "")

    def test_echec_powershell_leve_avec_le_message_stderr(self):
        resultat = mock.Mock(returncode=1, stdout="", stderr="Add-Type : introuvable")
        with mock.patch.object(runtime, "lancer", return_value=resultat):
            with self.assertRaises(RuntimeError) as capture:
                serve._choisir_dossier_windows("C:\\depart")
        self.assertIn("Add-Type", str(capture.exception))

    def test_apostrophe_dans_le_chemin_de_depart_ne_casse_pas_le_script(self):
        # PowerShell échappe un guillemet simple littéral en le doublant à
        # l'intérieur d'une chaîne à guillemets simples : un chemin qui en
        # contient un (rare, mais un nom de dossier utilisateur peut en
        # avoir un) ne doit pas casser la commande générée.
        resultat = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(runtime, "lancer", return_value=resultat) as appel:
            serve._choisir_dossier_windows("C:\\Users\\O'Brien")
        script = appel.call_args[0][0][-1]
        self.assertIn("O''Brien", script)


if __name__ == "__main__":
    unittest.main()
