"""« Vérifier les mises à jour » (issue #35) : GitHub tout de suite, sans
attendre que le cache vieillisse, et la page sait distinguer « à jour » de
« GitHub injoignable ». Dossier de données temporaire, GitHub simulé."""
import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import maj


class TestVerifierMaintenant(unittest.TestCase):
    def setUp(self):
        dossier = tempfile.TemporaryDirectory()
        self.addCleanup(dossier.cleanup)
        self.dossier = Path(dossier.name)
        for correctif in (
                mock.patch.object(maj.runtime, "app_dir", return_value=self.dossier),
                mock.patch.object(maj.runtime, "build_windows7", return_value=False),
                mock.patch.object(maj, "_version_locale", return_value="0.14.5"),
                mock.patch.object(maj, "_archive_de_ce_systeme", return_value={})):
            correctif.start()
            self.addCleanup(correctif.stop)

    def cache_frais(self, version):
        # Un cache vérifié il y a une minute : disponible() ne demanderait rien.
        (self.dossier / maj.CACHE).write_text(json.dumps(
            {"verifie": time.time() - 60, "version": version}), encoding="utf-8")

    def test_interroge_github_meme_avec_un_cache_frais(self):
        self.cache_frais("0.14.5")
        with mock.patch.object(maj, "_interroger", return_value={
                "tag_name": "v0.14.6", "html_url": "https://example/rel"}) as interroger:
            neuve, ok = maj.verifier_maintenant()
        interroger.assert_called_once()
        self.assertTrue(ok)
        self.assertEqual(neuve["version"], "0.14.6")

    def test_deja_a_jour(self):
        with mock.patch.object(maj, "_interroger", return_value={"tag_name": "v0.14.5"}):
            self.assertEqual(maj.verifier_maintenant(), ({}, True))

    def test_github_injoignable_est_dit_et_le_cache_reste(self):
        self.cache_frais("0.14.6")
        with mock.patch.object(maj, "_interroger",
                               side_effect=urllib.error.URLError("hors ligne")):
            neuve, ok = maj.verifier_maintenant()
        self.assertFalse(ok)
        self.assertEqual(neuve["version"], "0.14.6")   # ce qu'on savait déjà

    def test_edition_windows7_ne_propose_rien(self):
        with mock.patch.object(maj.runtime, "build_windows7", return_value=True), \
                mock.patch.object(maj, "_interroger") as interroger:
            self.assertEqual(maj.verifier_maintenant(), ({}, True))
        interroger.assert_not_called()

    def test_une_heure_entre_deux_visites_du_fil_de_fond(self):
        self.assertEqual(maj.FRAICHEUR, 3600)


if __name__ == "__main__":
    unittest.main()
