"""Démarrage automatique sous macOS : un « stop » ne doit pas être défait par
launchd (issue #31).

launchd ne tient pour voulue qu'une sortie de code 0 : l'agent ne relance
donc qu'après un échec, et le superviseur fait du SIGTERM de « stop » une
sortie 0. Vérifié sur un runner macOS : avec KeepAlive=true comme avec
SuccessfulExit=false seul, un processus tué par SIGTERM était relancé.
"""

import contextlib
import io
import os
import plistlib
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_IMPORT_HOME = tempfile.TemporaryDirectory(prefix="blink-sigterm-import-")
with mock.patch.dict(os.environ, {"BLINK_BOOTSTRAP": "none",
                                  "BLINK_HOME": _IMPORT_HOME.name}):
    import autostart

RACINE = Path(__file__).resolve().parent
POSIX = hasattr(signal, "pthread_sigmask")


def _environnement(dossier: str) -> dict:
    return dict(os.environ, BLINK_BOOTSTRAP="none", BLINK_HOME=dossier,
                PYTHONUTF8="1")


class AgentMacosTests(unittest.TestCase):
    def test_agent_ne_relance_qu_apres_un_echec(self):
        with tempfile.TemporaryDirectory() as dossier, \
                mock.patch.dict(os.environ, {"BLINK_HOME": dossier}), \
                contextlib.redirect_stdout(io.StringIO()) as sortie:
            self.assertEqual(autostart._macos("on", True), 0)
        # Simulation : « Écrirait <cible> : » puis le contenu du fichier.
        contenu = sortie.getvalue().split("\n", 1)[1]
        agent = plistlib.loads(contenu.encode("utf-8"))
        self.assertEqual(agent["KeepAlive"], {"SuccessfulExit": False})
        self.assertIs(agent["RunAtLoad"], True)


@unittest.skipUnless(POSIX, "signaux POSIX : Linux et macOS seulement")
class SigtermTests(unittest.TestCase):
    def test_sigterm_du_superviseur_devient_une_sortie_zero(self):
        # Le fil principal reste occupé, comme retenu par AppKit : seul le fil
        # dédié peut répondre au signal.
        script = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(RACINE)!r})\n"
            "import blink_cli\n"
            "blink_cli._sortie_propre_sur_sigterm()\n"
            "print('pret', flush=True)\n"
            "while True:\n"
            "    time.sleep(0.1)\n"
        )
        with tempfile.TemporaryDirectory() as dossier:
            processus = subprocess.Popen(
                [sys.executable, "-c", script], stdout=subprocess.PIPE,
                text=True, env=_environnement(dossier))
            try:
                self.assertEqual(processus.stdout.readline().strip(), "pret")
                processus.send_signal(signal.SIGTERM)
                code = processus.wait(timeout=20)
            finally:
                if processus.poll() is None:
                    processus.kill()
                processus.stdout.close()
        self.assertEqual(code, 0, "tué par le signal au lieu de sortir en 0")

    def test_un_blocage_herite_est_leve_au_demarrage(self):
        # Un processus lancé par le superviseur naît avec SIGTERM bloqué :
        # runtime le lève dès son import. Le témoin, sans runtime, prouve
        # que le blocage est bien hérité.
        enfant = (
            "import signal, sys\n"
            f"sys.path.insert(0, {str(RACINE)!r})\n"
            "if sys.argv[1] == 'runtime':\n"
            "    import runtime\n"
            "print(signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, []))\n"
        )
        parent = (
            "import signal, subprocess, sys\n"
            "signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})\n"
            "for mode in ('temoin', 'runtime'):\n"
            "    r = subprocess.run([sys.executable, '-c', sys.argv[1], mode],\n"
            "                       capture_output=True, text=True)\n"
            "    print(mode, r.stdout.strip(), r.stderr.strip()[-300:])\n"
        )
        with tempfile.TemporaryDirectory() as dossier:
            resultat = subprocess.run(
                [sys.executable, "-c", parent, enfant], capture_output=True,
                text=True, env=_environnement(dossier), timeout=120)
        lignes = dict(ligne.split(" ", 2)[:2] for ligne in resultat.stdout.splitlines()
                      if ligne.strip())
        self.assertEqual(lignes.get("temoin"), "True", resultat.stdout + resultat.stderr)
        self.assertEqual(lignes.get("runtime"), "False", resultat.stdout + resultat.stderr)


if __name__ == "__main__":
    unittest.main()
