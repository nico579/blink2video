"""known_identities() valide chaque requête /media et /thumb d'un clip : elle
ne doit relire le registre que lorsqu'il a changé (audit du 2026-09-24 : un
parse complet par vignette ou par requête Range, ~27 ms pour 10 000 clips)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-known-identities-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import serve  # noqa: E402 - bootstrap neutralise avant import


def entree(chemin: str) -> dict:
    return {"camera": "Salon", "created_at": "2026-09-01T10:00:00+00:00",
            "path": chemin}


class CacheIdentitesConnuesTests(unittest.TestCase):
    def setUp(self) -> None:
        dossier = tempfile.TemporaryDirectory(prefix="blink_identites_")
        self.addCleanup(dossier.cleanup)
        self.paths = {"input": Path(dossier.name)}
        self.registre = self.paths["input"] / serve.md.DOWNLOAD_STATE
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(serve, "_IDENTITES_CONNUES", (None, frozenset())).start()
        self.lectures = mock.patch.object(
            serve, "read_entries", wraps=serve.read_entries).start()

    def ecrire(self, chemins: list) -> None:
        serve.md.save_json(self.registre, {
            "version": 2,
            "clips": {f"k{i}": entree(c) for i, c in enumerate(chemins)},
        })

    def test_registre_inchange_lu_une_seule_fois(self):
        self.ecrire(["Salon/2026-09/a.mp4"])
        for _ in range(5):
            self.assertEqual(serve.known_identities(self.paths),
                             frozenset({"Salon/2026-09/a.mp4"}))
        self.assertEqual(self.lectures.call_count, 1)

    def test_registre_remplace_relu_aussitot(self):
        self.ecrire(["Salon/2026-09/a.mp4"])
        serve.known_identities(self.paths)
        self.ecrire(["Salon/2026-09/a.mp4", "Salon/2026-09/b.mp4"])
        self.assertIn("Salon/2026-09/b.mp4", serve.known_identities(self.paths))
        self.assertEqual(self.lectures.call_count, 2)

    def test_meme_taille_meme_date_mais_autre_fichier_relu(self):
        # Remplacement atomique : l'inode change même si taille et date
        # coïncidaient (horloge grossière d'un système de fichiers FAT).
        self.ecrire(["Salon/2026-09/a.mp4"])
        serve.known_identities(self.paths)
        avant = self.registre.stat()
        self.ecrire(["Salon/2026-09/c.mp4"])
        os.utime(self.registre, ns=(avant.st_atime_ns, avant.st_mtime_ns))
        self.assertEqual(serve.known_identities(self.paths),
                         frozenset({"Salon/2026-09/c.mp4"}))

    def test_registre_absent_vide_et_jamais_memorise(self):
        self.assertEqual(serve.known_identities(self.paths), frozenset())
        self.assertEqual(serve._IDENTITES_CONNUES, (None, frozenset()))
        self.ecrire(["Salon/2026-09/a.mp4"])
        self.assertEqual(serve.known_identities(self.paths),
                         frozenset({"Salon/2026-09/a.mp4"}))

    def test_registre_corrompu_reste_une_erreur(self):
        self.registre.write_text(json.dumps({"clips": []}), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            serve.known_identities(self.paths)


if __name__ == "__main__":
    unittest.main()
