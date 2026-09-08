"""Contrats des petits fichiers runtime, sur des destinations temporaires."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import runtime


class TestsEcrituresAtomiques(unittest.TestCase):
    def setUp(self):
        temporaire = tempfile.TemporaryDirectory(prefix="blink-ecritures-")
        self.addCleanup(temporaire.cleanup)
        self.racine = Path(temporaire.name)
        self.donnees = self.racine / "donnees"
        self.controle = self.racine / "controle"
        self.donnees.mkdir()
        self.controle.mkdir()
        self.patch = mock.patch.object(runtime, "app_dir", return_value=self.donnees)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        controle = mock.patch.object(runtime, "_dossier_controle", return_value=self.controle)
        controle.start()
        self.addCleanup(controle.stop)

    def ecritures(self):
        fiche = {"name": "Entrée", "enfants": [12, 34]}
        return (
            (self.donnees / runtime.REGLAGES,
             lambda: runtime.ecrire_reglages(**runtime.REGLAGES_DEFAUT),
             json.dumps(runtime.REGLAGES_DEFAUT)),
            (self.donnees / runtime.LANGUE, lambda: runtime.ecrire_langue("en"), "en"),
            (self.donnees / runtime.SUPPRESSION_AUTO,
             lambda: runtime.ecrire_suppression_auto({"Jardin", "Entrée"}),
             json.dumps(["Entrée", "Jardin"], ensure_ascii=False)),
            (self.controle / "instance.json",
             lambda: runtime._ecrire_fiche(self.controle / "instance.json", fiche),
             json.dumps(fiche, ensure_ascii=False)),
            (self.controle / runtime.MARQUEUR_CONFIGURATION_EN_ATTENTE,
             lambda: runtime._ecrire_marqueur_configuration(
                 runtime.MARQUEUR_CONFIGURATION_EN_ATTENTE), runtime.VERSION),
        )

    def test_contenu_utf8_et_destination_conserves(self):
        for cible, ecrire, attendu in self.ecritures():
            with self.subTest(cible=cible.name):
                ecrire()
                self.assertEqual(cible.read_text(encoding="utf-8"), attendu)
                self.assertEqual(list(self.racine.rglob("*.tmp")), [])

    def test_ancienne_version_reste_lisible_jusqu_au_remplacement(self):
        remplacer = Path.replace
        for cible, ecrire, attendu in self.ecritures():
            with self.subTest(cible=cible.name):
                cible.write_text("ancienne version", encoding="utf-8")
                remplacements = []

                def verifier(source, destination):
                    self.assertEqual(destination, cible)
                    self.assertEqual(source.parent, cible.parent)
                    self.assertNotEqual(source, cible)
                    self.assertEqual(cible.read_text(encoding="utf-8"), "ancienne version")
                    self.assertEqual(source.read_text(encoding="utf-8"), attendu)
                    remplacements.append(source)
                    return remplacer(source, destination)

                with mock.patch.object(Path, "replace", verifier):
                    ecrire()
                self.assertEqual(len(remplacements), 1)
                self.assertFalse(remplacements[0].exists())

    def test_ecriture_partielle_refusee_preserve_la_cible_et_nettoie(self):
        ecrire_texte = Path.write_text
        for cible, ecrire, _ in self.ecritures():
            with self.subTest(cible=cible.name):
                cible.write_text("ancienne version", encoding="utf-8")
                erreur = PermissionError("écriture interrompue")

                def interrompre(source, contenu, *args, **kwargs):
                    self.assertNotEqual(source, cible)
                    ecrire_texte(source, contenu[:2], *args, **kwargs)
                    raise erreur

                with mock.patch.object(Path, "write_text", interrompre):
                    with self.assertRaises(PermissionError) as recue:
                        ecrire()
                self.assertIs(recue.exception, erreur)
                self.assertEqual(cible.read_text(encoding="utf-8"), "ancienne version")
                self.assertEqual(list(self.racine.rglob("*.tmp")), [])

    def test_remplacement_refuse_preserve_la_cible_et_nettoie(self):
        for cible, ecrire, _ in self.ecritures():
            with self.subTest(cible=cible.name):
                cible.write_text("ancienne version", encoding="utf-8")
                erreur = PermissionError("destination verrouillée")
                with mock.patch.object(Path, "replace", side_effect=erreur):
                    with self.assertRaises(PermissionError) as recue:
                        ecrire()
                self.assertIs(recue.exception, erreur)
                self.assertEqual(cible.read_text(encoding="utf-8"), "ancienne version")
                self.assertEqual(list(self.racine.rglob("*.tmp")), [])

    def test_deux_ecritures_imbriquees_ont_des_temporaires_distincts(self):
        remplacer = Path.replace
        for cible, ecrire, attendu in self.ecritures():
            with self.subTest(cible=cible.name):
                sources = []

                def imbriquer(source, destination):
                    sources.append(source)
                    if len(sources) == 1:
                        ecrire()
                        self.assertTrue(source.exists())
                    return remplacer(source, destination)

                with mock.patch.object(Path, "replace", imbriquer):
                    ecrire()
                self.assertEqual(len(set(sources)), 2)
                self.assertEqual(cible.read_text(encoding="utf-8"), attendu)
                self.assertEqual(list(self.racine.rglob("*.tmp")), [])

    def test_reglages_dans_destination_explicite_sans_changer_la_racine_active(self):
        runtime.ecrire_reglages(**runtime.REGLAGES_DEFAUT, dossier=self.controle)
        self.assertTrue((self.controle / runtime.REGLAGES).is_file())
        self.assertFalse((self.donnees / runtime.REGLAGES).exists())
        self.assertEqual(runtime.app_dir(), self.donnees)

    def test_fiche_ne_cree_pas_silencieusement_un_dossier_absent(self):
        cible = self.racine / "absent" / "instance.json"
        with self.assertRaises(FileNotFoundError):
            runtime._ecrire_fiche(cible, {})
        self.assertFalse(cible.parent.exists())

    def test_marqueur_conserve_la_creation_de_la_racine_de_controle(self):
        controle = self.racine / "nouveau-controle"
        with mock.patch.object(runtime, "_dossier_controle", return_value=controle):
            runtime._ecrire_marqueur_configuration(runtime.MARQUEUR_CONFIGURATION_EN_ATTENTE)
        self.assertEqual(
            (controle / runtime.MARQUEUR_CONFIGURATION_EN_ATTENTE).read_text(encoding="utf-8"),
            runtime.VERSION)
        self.assertFalse((self.donnees / runtime.MARQUEUR_CONFIGURATION_EN_ATTENTE).exists())


if __name__ == "__main__":
    unittest.main()
