"""« Vérifier les mises à jour » (issue #35) : GitHub tout de suite, sans
attendre que le cache vieillisse, et la page sait distinguer « à jour » de
« GitHub injoignable ». La recherche de version elle-même (une question par
heure, comparaison des numéros, brouillons et préversions écartés, cache sur
disque) est nico579_commons.maj, testée là-bas ; ici, ce que blink2video y
ajoute : la version lue sur disque depuis les sources, l'archive de ce
système, le dossier de données et l'édition Windows 7. Dossier de données
temporaire, GitHub simulé."""
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
        maj._verificateur.cache_clear()
        self.addCleanup(maj._verificateur.cache_clear)
        for correctif in (
                mock.patch.object(maj.runtime, "app_dir", return_value=self.dossier),
                mock.patch.object(maj.runtime, "build_windows7", return_value=False),
                mock.patch.object(maj, "_version_locale", return_value="0.14.5"),
                mock.patch.object(maj, "_archive_de_ce_systeme", return_value={})):
            correctif.start()
            self.addCleanup(correctif.stop)

    def cache_frais(self, version):
        # Une réponse reçue il y a une minute : disponible() ne demanderait rien.
        (self.dossier / maj.CACHE).write_text(json.dumps(
            {"verifie": time.time() - 60, "version": version, "page": "https://x/r",
             "assets": []}), encoding="utf-8")

    def test_interroge_github_meme_avec_un_cache_frais(self):
        self.cache_frais("0.14.5")
        with mock.patch.object(maj, "_ouvrir_github", return_value={
                "tag_name": "v0.14.6", "html_url": "https://example/rel"}) as interroger:
            neuve, ok = maj.verifier_maintenant()
        interroger.assert_called_once()
        self.assertTrue(ok)
        self.assertEqual(neuve["version"], "0.14.6")

    def test_deja_a_jour(self):
        with mock.patch.object(maj, "_ouvrir_github", return_value={"tag_name": "v0.14.5"}):
            self.assertEqual(maj.verifier_maintenant(), ({}, True))

    def test_github_injoignable_est_dit_et_le_cache_reste(self):
        self.cache_frais("0.14.6")
        with mock.patch.object(maj, "_ouvrir_github",
                               side_effect=urllib.error.URLError("hors ligne")):
            neuve, ok = maj.verifier_maintenant()
        self.assertFalse(ok)
        self.assertEqual(neuve["version"], "0.14.6")   # ce qu'on savait déjà

    def test_edition_windows7_ne_propose_rien(self):
        with mock.patch.object(maj.runtime, "build_windows7", return_value=True), \
                mock.patch.object(maj, "_ouvrir_github") as interroger:
            self.assertEqual(maj.verifier_maintenant(), ({}, True))
            self.assertEqual(maj.disponible(force=True), {})
        interroger.assert_not_called()

    def test_une_heure_entre_deux_visites_du_fil_de_fond(self):
        self.assertEqual(maj.FRAICHEUR, 3600)
        self.assertEqual(maj._verificateur().fraicheur_s, 3600)

    def test_cache_frais_evite_le_reseau_mais_pas_un_cache_vieux(self):
        self.cache_frais("0.14.6")
        with mock.patch.object(maj, "_ouvrir_github") as interroger:
            self.assertEqual(maj.disponible()["version"], "0.14.6")
        interroger.assert_not_called()
        # Une réponse vieille de plus d'une heure : on redemande.
        (self.dossier / maj.CACHE).write_text(json.dumps(
            {"verifie": time.time() - 7200, "version": "0.14.6", "assets": []}), encoding="utf-8")
        maj._verificateur.cache_clear()
        with mock.patch.object(maj, "_ouvrir_github", return_value={"tag_name": "v0.14.7"}) as interroger:
            self.assertEqual(maj.disponible()["version"], "0.14.7")
        interroger.assert_called_once()

    def test_sans_reseau_ne_demande_jamais_a_github(self):
        with mock.patch.object(maj, "_ouvrir_github") as interroger:
            self.assertEqual(maj.disponible(reseau=False), {})
        interroger.assert_not_called()

    def test_archive_choisie_a_partir_des_fichiers_de_la_release(self):
        with mock.patch.object(maj, "_ouvrir_github", return_value={
                "tag_name": "v0.14.6", "assets": [{"name": "a.zip", "size": 3}]}), \
                mock.patch.object(maj, "_archive_de_ce_systeme", return_value={"nom": "a.zip"}) as choisir:
            neuve, _ = maj.verifier_maintenant()
        self.assertEqual(neuve["archive"], {"nom": "a.zip"})
        self.assertEqual(choisir.call_args.args[0][0]["name"], "a.zip")

    def test_la_version_locale_est_relue_a_chaque_question(self):
        # Depuis les sources, le fichier VERSION peut changer sous un processus
        # lancé : une version déjà installée ne doit pas passer pour à venir.
        with mock.patch.object(maj, "_ouvrir_github", return_value={"tag_name": "v0.14.6"}):
            maj.verifier_maintenant()
        with mock.patch.object(maj, "_version_locale", return_value="0.14.6"):
            self.assertEqual(maj.disponible(reseau=False), {})


if __name__ == "__main__":
    unittest.main()
