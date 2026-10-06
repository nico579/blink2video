"""« Vérifier les mises à jour » (issue #35) : GitHub tout de suite, et la page
sait distinguer « à jour » de « GitHub injoignable ». La recherche de version
elle-même (une question par heure, comparaison des numéros, brouillons et
préversions écartés) est nico579_commons.maj, testée là-bas ; ici, ce que
blink2video y ajoute : la version lue sur disque depuis les sources, l'archive
de ce système et l'édition Windows 7. GitHub simulé."""
import unittest
import urllib.error
from unittest import mock

from nico579_commons import maj as maj_commune

import maj


def _verificateur(reponse):
    """Un Verificateur dont GitHub répond `reponse` (dict, ou exception levée)."""
    def ouvrir(url):
        if isinstance(reponse, Exception):
            raise reponse
        return reponse
    return maj_commune.Verificateur(maj.DEPOT, "0.14.5", ouvrir=ouvrir)


class TestVerifierMaintenant(unittest.TestCase):
    def setUp(self):
        for correctif in (
                mock.patch.object(maj.runtime, "build_windows7", return_value=False),
                mock.patch.object(maj, "_version_locale", return_value="0.14.5"),
                mock.patch.object(maj, "_archive_de_ce_systeme", return_value={})):
            correctif.start()
            self.addCleanup(correctif.stop)

    def avec(self, reponse):
        patch = mock.patch.object(maj, "VERIFICATEUR", _verificateur(reponse))
        patch.start()
        self.addCleanup(patch.stop)

    def test_interroge_github_et_rend_la_version_plus_recente(self):
        self.avec({"tag_name": "v0.14.6", "html_url": "https://example/rel", "assets": []})
        neuve, ok = maj.verifier_maintenant()
        self.assertTrue(ok)
        self.assertEqual(neuve["version"], "0.14.6")
        self.assertEqual(neuve["page"], "https://example/rel")
        self.assertEqual(neuve["archive"], {})

    def test_deja_a_jour(self):
        self.avec({"tag_name": "v0.14.5"})
        self.assertEqual(maj.verifier_maintenant(), ({}, True))

    def test_github_injoignable_est_dit_et_la_reponse_precedente_reste(self):
        self.avec({"tag_name": "v0.14.6"})
        maj.verifier_maintenant()
        maj.VERIFICATEUR._ouvrir = lambda url: (_ for _ in ()).throw(urllib.error.URLError("hors ligne"))
        neuve, ok = maj.verifier_maintenant()
        self.assertFalse(ok)
        self.assertEqual(neuve["version"], "0.14.6")   # ce qu'on savait déjà

    def test_disponible_ne_touche_jamais_le_reseau(self):
        self.avec(AssertionError("la page ne doit pas interroger GitHub"))
        self.assertEqual(maj.disponible(), {})

    def test_archive_construite_a_partir_des_fichiers_de_la_release(self):
        self.avec({"tag_name": "v0.14.6", "assets": [
            {"name": "a.zip", "browser_download_url": "https://x/a.zip", "size": 3}]})
        with mock.patch.object(maj, "_archive_de_ce_systeme", return_value={"nom": "a.zip"}) as choisir:
            neuve, _ = maj.verifier_maintenant()
        self.assertEqual(neuve["archive"], {"nom": "a.zip"})
        self.assertEqual(choisir.call_args.args[0][0]["name"], "a.zip")

    def test_la_version_locale_est_relue_a_chaque_question(self):
        # Depuis les sources, le fichier VERSION peut changer sous un processus
        # lancé : une version déjà installée ne doit pas passer pour à venir.
        self.avec({"tag_name": "v0.14.6"})
        maj.verifier_maintenant()
        with mock.patch.object(maj, "_version_locale", return_value="0.14.6"):
            self.assertEqual(maj.disponible(), {})

    def test_edition_windows7_ne_propose_rien(self):
        with mock.patch.object(maj.runtime, "build_windows7", return_value=True), \
                mock.patch.object(maj.VERIFICATEUR, "verifier") as verifier:
            self.assertEqual(maj.verifier_maintenant(), ({}, True))
            self.assertEqual(maj.disponible(), {})
        verifier.assert_not_called()

    def test_une_heure_entre_deux_visites_du_fil_de_fond(self):
        self.assertEqual(maj.VERIFICATEUR.fraicheur_s, 3600)


if __name__ == "__main__":
    unittest.main()
