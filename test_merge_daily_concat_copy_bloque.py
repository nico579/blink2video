"""concat_copy() (assemblage hebdomadaire/mensuel, chemin copie de flux)
n'avait aucune protection contre un ffmpeg bloqué, contrairement à sa
fonction jumelle run_ffmpeg_batch, déjà corrigée pour ce risque documenté
(AUDIT-2026-08-13, 28.82 : un ffmpeg bloqué a déjà gelé toute la boucle merge
en silence, sans erreur ni log, jusqu'à un arrêt manuel)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import merge_daily


class TestsConcatCopyBloque(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="blink_concat_bloque_")
        self.addCleanup(self.tmp.cleanup)
        self.racine = Path(self.tmp.name)

    def test_ffmpeg_bloque_est_tue_et_signale_au_lieu_de_geler(self):
        part = self.racine / "a.mp4"
        part.write_bytes(b"peu importe : le faux processus ne le lit jamais")
        destination = self.racine / "sortie.mp4"

        processus = mock.MagicMock()
        processus.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=0.3),
            ("", ""),
        ]
        processus.returncode = 0

        with mock.patch.object(merge_daily, "SILENCE_MAX", 0.3), \
             mock.patch.object(merge_daily.runtime, "demarrer",
                               return_value=processus), \
             mock.patch.object(merge_daily.runtime, "inscrire_travailleur") as inscrire, \
             mock.patch.object(merge_daily.runtime, "retirer_travailleur") as retirer:
            ok, message = merge_daily.concat_copy("ffmpeg-factice", [part], destination)

        self.assertFalse(ok)
        self.assertIn("silencieux", message)
        processus.kill.assert_called_once()
        self.assertEqual(processus.communicate.call_count, 2)
        inscrire.assert_called_once_with(processus.pid)
        retirer.assert_called_once_with(processus.pid)

    def test_ffmpeg_normal_nest_pas_affecte(self):
        """Non-régression : le chemin heureux reste inchangé après l'ajout
        du timeout."""
        part = self.racine / "a.mp4"
        part.write_bytes(b"peu importe")
        destination = self.racine / "sortie.mp4"

        processus = mock.MagicMock()
        processus.communicate.return_value = ("", "")
        processus.returncode = 0

        with mock.patch.object(merge_daily.runtime, "demarrer",
                               return_value=processus), \
             mock.patch.object(merge_daily.runtime, "inscrire_travailleur"), \
             mock.patch.object(merge_daily.runtime, "retirer_travailleur"), \
             mock.patch.object(merge_daily, "valid_mp4", return_value=True):
            ok, message = merge_daily.concat_copy("ffmpeg-factice", [part], destination)

        self.assertTrue(ok)
        self.assertEqual(message, "")
        processus.kill.assert_not_called()
        processus.communicate.assert_called_once_with(timeout=merge_daily.SILENCE_MAX)


if __name__ == "__main__":
    unittest.main(verbosity=2)
