"""Non-régression : progress_printer() sous pythonw.exe (AUDIT-2026-08-23
avait déjà traité l'absence de console pour les messages de statut, via
journal() dans runtime.py, mais pas les impressions internes à merge_daily.py
elles-mêmes). print() ne plante jamais avec sys.stdout à None (repli
silencieux documenté de CPython), mais sys.stdout.isatty() si : la fusion
échouait dès son premier lot sous démarrage automatique (pythonw.exe),
en boucle toutes les 5 minutes, sans jamais rien encoder (repéré en réel
le 27 août 2026, après plusieurs heures silencieuses)."""

from __future__ import annotations

import sys
import unittest
from unittest import mock

import merge_daily


class TestsProgressPrinterSansConsole(unittest.TestCase):
    def test_stdout_none_ne_leve_pas(self):
        with mock.patch.object(sys, "stdout", None):
            rapporteur = merge_daily.progress_printer("[1/3]")
            rapporteur(0.0)
            rapporteur(0.5)
            rapporteur(1.0)

    def test_stdout_reel_continue_de_fonctionner(self):
        import io
        faux_stdout = io.StringIO()
        with mock.patch.object(sys, "stdout", faux_stdout):
            rapporteur = merge_daily.progress_printer("[1/3]")
            rapporteur(1.0)
        self.assertIn("100", faux_stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
