"""Une vraie mise à jour rend à blink2video tous ses verbes.

Le maillon qui a cassé le 2026-10-17 n'était couvert par aucun test : la
relance par l'assistant externe de nico579-commons (PowerShell sous Windows, sh
ailleurs), qui perdait l'argument vide de « --trusted-host "" ». Page et relève
ne revenaient pas après la première vraie mise à jour (0.23.0 vers 0.23.1).

Ici, le vrai assistant, celui du commun que blink2video embarque, conduit une
transaction complète : un processus parent qui lui confie la relance puis
s'arrête, l'échange des dossiers, la relance d'un faux programme qui note ses
arguments exacts. blink2video doit y retrouver chacun de ses verbes."""

from __future__ import annotations  # Python 3.8 (build Windows 7)

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import maj
import runtime

# Ce qui tournait chez Nico avant la mise à jour du 2026-10-17 (« start »).
GROUPES = [["serve", "--port", "8765", "--timezone", "Europe/Paris", "--trusted-host", ""],
           ["watch", "--loop", "10"],
           ["download", "--from", "all", "--usb-loop", "10", "--cloud-loop", "1"]]

PARENT = r'''
import json, sys
from pathlib import Path
from nico579_commons import maj_install as mi
d = json.loads(sys.argv[1])
disp = mi.Disposition(system=d["system"], machine="x86_64", asset_name="a", archive_kind="zip",
                      expected_root="blink2video", install_root=Path(d["install"]),
                      data_relative=Path("."), executable_relative=Path(d["exe"]))
prep = mi.Preparation(version="9.9.9", token=d["token"], disposition=disp,
                      staging_root=Path(d["staging"]), payload_root=Path(d["payload"]),
                      backup_root=Path(d["backup"]), failed_root=Path(d["failed"]))
mi.lancer(mi.Application("blink2video"), prep, verbes=d["verbes"], ecrire=lambda *_: None)
mi.valider(prep)
'''

# Le faux programme note la liste exacte de ses arguments, puis vit assez
# longtemps pour que l'assistant le juge bien démarré (il vérifie après 5 s).
NOTE = r'''
import json, sys, time
from pathlib import Path
Path(__file__).with_name("argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
time.sleep(12)
'''


def faux_programme(dossier: Path) -> str:
    if os.name == "nt":
        (dossier / "note_argv.py").write_text(NOTE, encoding="utf-8")
        (dossier / "blink2video.cmd").write_text(
            f'@"{sys.executable}" "%~dp0note_argv.py" %*\r\n', encoding="utf-8")
        return "blink2video.cmd"
    exe = dossier / "blink2video"
    exe.write_text(f"#!{sys.executable}\n{NOTE}", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return "blink2video"


def supprimer(chemin: Path, delai_s: float = 30) -> None:
    """rmtree qui patiente : sous Windows, un dossier reste verrouillé tant
    qu'un processus lancé dedans tourne encore."""
    fin = time.monotonic() + delai_s
    while chemin.exists():
        try:
            shutil.rmtree(chemin)
        except OSError:
            if time.monotonic() > fin:
                shutil.rmtree(chemin, ignore_errors=True)
                return
            time.sleep(0.5)


class RelanceReelle(unittest.TestCase):
    def setUp(self):
        self.racine = Path(tempfile.mkdtemp(prefix="maj-relance-")).resolve()
        self.addCleanup(supprimer, self.racine)

    def relancer(self, verbes: list) -> list:
        """Conduit la transaction et rend les arguments reçus par la relance."""
        install = self.racine / "blink2video"
        install.mkdir()
        exe = faux_programme(install)
        staging = self.racine / ".blink2video.update-abc"
        payload = staging / "extracted" / "blink2video"
        payload.mkdir(parents=True)
        faux_programme(payload)
        donnees = {"system": "Windows" if os.name == "nt" else "Linux",
                   "install": str(install), "exe": exe, "verbes": verbes,
                   "staging": str(staging), "payload": str(payload),
                   "backup": str(self.racine / ".blink2video.backup-t0k3n"),
                   "failed": str(self.racine / ".blink2video.failed-t0k3n"),
                   # Sous systemd (runner Linux), l'assistant tourne dans une unité
                   # nommée d'après ce jeton : un par essai.
                   "token": uuid.uuid4().hex}
        parent = subprocess.run([sys.executable, "-c", PARENT, json.dumps(donnees)],
                                capture_output=True, text=True, timeout=90)
        self.assertEqual(parent.returncode, 0, parent.stdout + parent.stderr)
        note = install / "argv.json"
        fin = time.monotonic() + 90
        while not note.exists() and time.monotonic() < fin:
            time.sleep(0.5)
        self.assertTrue(note.exists(), "la nouvelle version n'a pas été relancée")
        # Laisser l'assistant conclure (il range son dossier) avant le ménage.
        while staging.exists() and time.monotonic() < fin:
            time.sleep(0.5)
        return json.loads(note.read_text(encoding="utf-8"))

    def test_la_ligne_de_relance_d_aujourd_hui_rend_tous_les_verbes(self):
        with mock.patch.object(maj, "_compositions_en_cours", return_value=[GROUPES]):
            lignes = maj._verbes_a_relancer()
        self.assertEqual(len(lignes), 1)
        recus = self.relancer(lignes)
        # Sans « --trusted-host "" » : serve retrouve cet hôte vide par défaut.
        self.assertEqual(runtime.decouper_verbes(recus), [GROUPES[0][:5], GROUPES[1], GROUPES[2]])

    def test_une_ligne_d_avant_0_24_1_est_reparee(self):
        # Ce que 0.23.0 à 0.24.0 confient à l'assistant : la chaîne vide comprise.
        recus = self.relancer([[mot for groupe in GROUPES for mot in groupe]])
        self.assertEqual(runtime.decouper_verbes(recus), GROUPES)


if __name__ == "__main__":
    unittest.main()
