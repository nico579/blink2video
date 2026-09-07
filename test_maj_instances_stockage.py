"""La mise à jour garde la racine de contrôle et toutes les compositions.

Les fiches vivent exclusivement dans des répertoires temporaires. Les
démarrages, arrêts, téléchargements et permutations sont tous simulés.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import maj
import runtime


class TestMiseAJourStockage(unittest.TestCase):
    def setUp(self):
        temporaire = tempfile.TemporaryDirectory(prefix="blink-maj-instances-")
        self.addCleanup(temporaire.cleanup)
        self.racine = Path(temporaire.name).resolve()
        self.installe = self.racine / "installation"
        self.donnees = self.racine / "donnees"
        self.preparation = self.racine / "preparation"
        for dossier in (self.installe, self.donnees, self.preparation):
            dossier.mkdir()
        (self.installe / runtime.POINTEUR_STOCKAGE).write_text(
            str(self.donnees), encoding="utf-8")
        self.pile = contextlib.ExitStack()
        self.addCleanup(self.pile.close)
        self.pile.enter_context(mock.patch.dict(os.environ))
        os.environ.pop("BLINK_HOME", None)
        os.environ.pop("BLINK_CONTROL_HOME", None)
        os.environ.pop("BLINK_UPDATE_AUTO_HOME", None)
        self.pile.enter_context(mock.patch.object(runtime, "frozen", return_value=True))
        self.pile.enter_context(mock.patch.object(
            runtime, "processus_correspond", return_value=True))
        self.pile.enter_context(mock.patch.object(
            runtime, "processus_vivant", return_value=True))
        self.pile.enter_context(mock.patch.object(
            runtime, "arreter_processus", side_effect=AssertionError("Arrêt réel interdit")))
        self.pile.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def preparer_finaliseur(self):
        """Capture l'environnement produit par le vrai premier temps."""
        with contextlib.ExitStack() as pile:
            for correctif in (
                mock.patch.object(maj.sys, "executable", str(self.installe / "blink2video.exe")),
                mock.patch.object(runtime, "build_windows7", return_value=False),
                mock.patch.object(maj, "disponible", return_value={
                    "version": "0.99.0", "archive": {
                        "nom": "blink2video.zip", "url": "https://example.invalid/blink2video.zip"}}),
                mock.patch.object(maj, "_nettoyer"),
                mock.patch.object(maj, "_empreinte_attendue", return_value="0" * 64),
                mock.patch.object(maj, "_creer_dossier_travail", return_value=self.preparation),
                mock.patch.object(maj, "_telecharger"),
                mock.patch.object(maj, "_extraire", return_value=self.preparation),
                mock.patch.object(maj, "_rendre_executable"),
                mock.patch.object(maj, "_verifier", return_value=True),
                mock.patch.object(runtime, "travail"),
                mock.patch.object(runtime, "fin_travail"),
            ):
                pile.enter_context(correctif)
            demarrer = pile.enter_context(mock.patch.object(runtime, "demarrer"))
            code = maj.installer()
            self.assertEqual(code, 0)
            demarrer.assert_called_once()
            arguments = demarrer.call_args.kwargs
            # Aucun enfant réel ne récupère cette poignée ouverte par installer.
            arguments["stdout"].close()
            return dict(arguments["env"])

    def test_pointeur_stockage_ne_deplace_pas_la_racine_de_controle(self):
        environnement = self.preparer_finaliseur()

        self.assertEqual(Path(environnement["BLINK_HOME"]), self.donnees)
        self.assertEqual(Path(environnement["BLINK_CONTROL_HOME"]), self.installe)
        self.assertEqual(environnement.get("BLINK_UPDATE_AUTO_HOME"), "1")
        with mock.patch.dict(os.environ, environnement, clear=True), \
                mock.patch.object(maj.sys, "executable", str(self.preparation / "blink2video.exe")):
            self.assertEqual(runtime.app_dir(), self.donnees)
            self.assertEqual(runtime._dossier_controle(), self.installe)

    def test_blink_home_utilisateur_est_conserve(self):
        force = self.racine / "stockage-force"
        force.mkdir()
        os.environ["BLINK_HOME"] = str(force)

        environnement = self.preparer_finaliseur()

        self.assertEqual(Path(environnement["BLINK_HOME"]), force)
        self.assertEqual(Path(environnement["BLINK_CONTROL_HOME"]), force)
        self.assertNotIn("BLINK_UPDATE_AUTO_HOME", environnement)

    def test_racine_de_controle_deja_transmise_est_conservee(self):
        controle = self.racine / "controle-force"
        controle.mkdir()
        os.environ["BLINK_CONTROL_HOME"] = str(controle)

        environnement = self.preparer_finaliseur()

        self.assertEqual(Path(environnement["BLINK_HOME"]), self.donnees)
        self.assertEqual(Path(environnement["BLINK_CONTROL_HOME"]), controle)

    def test_finaliseur_retrouve_instance_active_et_refuse_permutation(self):
        dossier_fiches = self.installe / runtime.INSTANCES
        dossier_fiches.mkdir()
        fiche = dossier_fiches / "123.json"
        fiche.write_text(json.dumps({"pid": 123, "verbes": [["serve"]]}), encoding="utf-8")
        environnement = self.preparer_finaliseur()

        with mock.patch.dict(os.environ, environnement, clear=True), \
                mock.patch.object(maj.sys, "executable", str(self.preparation / "blink2video.exe")), \
                mock.patch.object(runtime, "lancer", return_value=mock.Mock(returncode=0)) as arreter, \
                mock.patch.object(maj.time, "sleep"), \
                mock.patch.object(maj, "_permuter") as permuter, \
                mock.patch.object(maj, "_relancer") as relancer:
            self.assertEqual([f["pid"] for f in runtime.lire_instances()], [123])
            code = maj.finaliser(self.installe)

        self.assertEqual(code, 1)
        self.assertTrue(fiche.exists())
        permuter.assert_not_called()
        relancer.assert_not_called()
        arreter.assert_called_once()
        self.assertEqual(Path(arreter.call_args.kwargs["env"]["BLINK_HOME"]), self.installe)

    def test_relance_retire_le_stockage_synthetique_et_suit_le_pointeur(self):
        environnement = self.preparer_finaliseur()
        nouveau = self.racine / "apres-mise-a-jour"
        nouveau.mkdir()

        with mock.patch.dict(os.environ, environnement, clear=True), \
                mock.patch.object(maj.sys, "executable", str(self.preparation / "blink2video.exe")), \
                mock.patch.object(runtime, "demarrer") as demarrer:
            maj._relancer(self.installe, [["serve"]])
            relance = demarrer.call_args.kwargs.get("env", dict(os.environ))

        self.assertNotIn("BLINK_HOME", relance)
        self.assertNotIn("BLINK_UPDATE_AUTO_HOME", relance)
        self.assertEqual(Path(relance["BLINK_CONTROL_HOME"]), self.installe)
        (self.installe / runtime.POINTEUR_STOCKAGE).write_text(str(nouveau), encoding="utf-8")
        with mock.patch.dict(os.environ, relance, clear=True), \
                mock.patch.object(maj.sys, "executable", str(self.installe / "blink2video.exe")):
            self.assertEqual(runtime.app_dir(), nouveau)
            self.assertEqual(runtime._dossier_controle(), self.installe)

    def test_relance_preserve_le_stockage_force_par_utilisateur(self):
        force = self.racine / "stockage-force"
        force.mkdir()
        os.environ["BLINK_HOME"] = str(force)
        environnement = self.preparer_finaliseur()

        with mock.patch.dict(os.environ, environnement, clear=True), \
                mock.patch.object(runtime, "demarrer") as demarrer:
            maj._relancer(self.installe, [["serve"]])
            relance = demarrer.call_args.kwargs.get("env", dict(os.environ))

        self.assertEqual(Path(relance["BLINK_HOME"]), force)
        self.assertEqual(Path(relance["BLINK_CONTROL_HOME"]), force)
        self.assertNotIn("BLINK_UPDATE_AUTO_HOME", relance)

    def test_ancien_installateur_sans_racine_de_controle_retrouve_instance(self):
        dossier_fiches = self.installe / runtime.INSTANCES
        dossier_fiches.mkdir()
        fiche = dossier_fiches / "123.json"
        fiche.write_text(json.dumps({"pid": 123, "verbes": [["serve"]]}), encoding="utf-8")
        # Les anciennes versions ne transmettaient que ce BLINK_HOME
        # synthétique, pas la racine des fiches à côté de l'installation.
        os.environ["BLINK_HOME"] = str(self.donnees)

        with mock.patch.object(maj.sys, "executable", str(self.preparation / "blink2video.exe")), \
                mock.patch.object(runtime, "lancer", return_value=mock.Mock(returncode=0)) as arreter, \
                mock.patch.object(maj.time, "sleep"), \
                mock.patch.object(maj, "_permuter") as permuter, \
                mock.patch.object(maj, "_relancer") as relancer:
            code = maj.finaliser(self.installe)

        self.assertEqual(code, 1)
        self.assertTrue(fiche.exists())
        permuter.assert_not_called()
        relancer.assert_not_called()
        arreter.assert_called_once()
        self.assertEqual(Path(arreter.call_args.kwargs["env"]["BLINK_HOME"]), self.installe)

    def test_ancien_installateur_refuse_racines_de_controle_ambigues(self):
        for dossier, pid in ((self.installe, 123), (self.donnees, 456)):
            dossier_fiches = dossier / runtime.INSTANCES
            dossier_fiches.mkdir()
            (dossier_fiches / (str(pid) + ".json")).write_text(
                json.dumps({"pid": pid, "verbes": [["serve"]]}), encoding="utf-8")
        os.environ["BLINK_HOME"] = str(self.donnees)

        with mock.patch.object(maj.sys, "executable", str(self.preparation / "blink2video.exe")), \
                mock.patch.object(runtime, "lancer") as arreter, \
                mock.patch.object(maj.time, "sleep"), \
                mock.patch.object(maj, "_permuter") as permuter, \
                mock.patch.object(maj, "_relancer") as relancer:
            code = maj.finaliser(self.installe)

        self.assertEqual(code, 1)
        arreter.assert_not_called()
        permuter.assert_not_called()
        relancer.assert_not_called()
        self.assertTrue((self.installe / runtime.INSTANCES / "123.json").exists())
        self.assertTrue((self.donnees / runtime.INSTANCES / "456.json").exists())


class TestMiseAJourCompositions(unittest.TestCase):
    def executer(self, fiches, permutation=True):
        with tempfile.TemporaryDirectory(prefix="blink-maj-compositions-") as dossier, \
                mock.patch.object(runtime, "frozen", return_value=False), \
                mock.patch.object(runtime, "lire_instances", side_effect=[fiches, []]), \
                mock.patch.object(runtime, "lancer", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(maj.time, "sleep"), \
                mock.patch.object(maj, "_permuter", return_value=permutation), \
                mock.patch.object(maj, "_relancer") as relancer, \
                contextlib.redirect_stdout(io.StringIO()):
            code = maj.finaliser(Path(dossier))
        return code, [appel.args[1] for appel in relancer.call_args_list]

    def test_relance_chaque_composition_distincte_apres_mise_a_jour(self):
        compositions = [
            [["serve", "--port", "8765"]],
            [["download", "--loop", "1"], ["merge", "--loop", "5"]],
            [["serve", "--port", "8766"]],
        ]
        fiches = [{"pid": index + 1, "verbes": verbes}
                  for index, verbes in enumerate(compositions)]

        code, relances = self.executer(fiches)

        self.assertEqual(code, 0)
        self.assertEqual(relances, compositions)

    def test_echec_permutation_relance_aussi_toutes_les_compositions(self):
        compositions = [[["serve"]], [["download", "--loop", "1"]]]
        fiches = [{"pid": index + 1, "verbes": verbes}
                  for index, verbes in enumerate(compositions)]

        code, relances = self.executer(fiches, permutation=False)

        self.assertEqual(code, 1)
        self.assertEqual(relances, compositions)

    def test_fiches_dupliquees_ne_relancent_pas_deux_fois_la_meme_composition(self):
        composition = [["serve", "--port", "8765"]]
        fiches = [{"pid": 1, "verbes": composition},
                  {"pid": 2, "verbes": [["serve", "--port", "8765"]]}]

        code, relances = self.executer(fiches)

        self.assertEqual(code, 0)
        self.assertEqual(relances, [composition])

    def test_sans_instance_conserve_la_relance_par_defaut(self):
        code, relances = self.executer([])

        self.assertEqual(code, 0)
        self.assertEqual(relances, [[]])


if __name__ == "__main__":
    unittest.main()
