"""La mise à jour garde la racine de contrôle et toutes les compositions.

Les fiches vivent exclusivement dans des répertoires temporaires, dossier
d'état compris. Les démarrages, arrêts, téléchargements et permutations sont
tous simulés.
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
        # Données d'une version ≤ 0.13 redirigées par son blink_home.txt.
        self.donnees = self.racine / "donnees"
        self.preparation = self.racine / "preparation"
        self.etat = self.racine / "etat"
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
        self.pile.enter_context(mock.patch.object(
            runtime, "_dossier_etat_standard", return_value=self.etat))
        self.pile.enter_context(mock.patch.object(runtime, "_ETATS_CREES", set()))
        self.pile.enter_context(mock.patch.object(runtime, "frozen", return_value=True))
        self.pile.enter_context(mock.patch.object(
            runtime, "processus_correspond", return_value=True))
        self.pile.enter_context(mock.patch.object(
            runtime, "processus_vivant", return_value=True))
        self.pile.enter_context(mock.patch.object(
            runtime, "arreter_processus", side_effect=AssertionError("Arrêt réel interdit")))
        self.pile.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def fiche(self, dossier: Path, pid: int = 123) -> Path:
        fiches = dossier / runtime.INSTANCES
        fiches.mkdir(parents=True, exist_ok=True)
        fiche = fiches / f"{pid}.json"
        fiche.write_text(json.dumps({"pid": pid, "verbes": [["serve"]]}), encoding="utf-8")
        return fiche

    def environnement_lanceur_013(self) -> dict:
        """Ce que passait le premier temps d'une version ≤ 0.13 : ses
        données (cible du pointeur) et ses fiches (à côté du programme)."""
        return dict(os.environ, BLINK_HOME=str(self.donnees),
                    BLINK_CONTROL_HOME=str(self.installe),
                    BLINK_UPDATE_AUTO_HOME="1")

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

    def relancer(self, environnement: dict) -> dict:
        with mock.patch.dict(os.environ, environnement, clear=True), \
                mock.patch.object(maj.sys, "executable", str(self.preparation / "blink2video.exe")), \
                mock.patch.object(runtime, "demarrer") as demarrer:
            maj._relancer(self.installe, [["serve"]])
            return demarrer.call_args.kwargs.get("env", dict(os.environ))

    def test_installateur_nomme_l_etat_et_ses_fiches(self):
        environnement = self.preparer_finaliseur()

        self.assertEqual(Path(environnement["BLINK_HOME"]), self.etat)
        self.assertEqual(Path(environnement["BLINK_CONTROL_HOME"]), self.etat)
        self.assertEqual(environnement.get("BLINK_UPDATE_AUTO_HOME"), "1")
        with mock.patch.dict(os.environ, environnement, clear=True), \
                mock.patch.object(maj.sys, "executable", str(self.preparation / "blink2video.exe")):
            self.assertEqual(runtime.app_dir(), self.etat)
            self.assertEqual(runtime._dossier_controle(), self.etat)

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

        self.assertEqual(Path(environnement["BLINK_HOME"]), self.etat)
        self.assertEqual(Path(environnement["BLINK_CONTROL_HOME"]), controle)

    def verifier_refus(self, environnement: dict, fiche: Path, racine_arret: Path) -> None:
        with mock.patch.dict(os.environ, environnement, clear=True), \
                mock.patch.object(maj.sys, "executable", str(self.preparation / "blink2video.exe")), \
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
        self.assertEqual(Path(arreter.call_args.kwargs["env"]["BLINK_HOME"]), racine_arret)

    def test_finaliseur_retrouve_instance_active_et_refuse_permutation(self):
        fiche = self.fiche(self.etat)
        self.verifier_refus(self.preparer_finaliseur(), fiche, self.etat)

    def test_finaliseur_retrouve_les_fiches_d_un_lanceur_013(self):
        """Première mise à jour vers 0.14 : les fiches de la version en cours
        sont encore à côté du programme, là où son lanceur les désigne."""
        fiche = self.fiche(self.installe)
        self.verifier_refus(self.environnement_lanceur_013(), fiche, self.installe)

    def test_relance_retire_les_racines_du_protocole(self):
        relance = self.relancer(self.preparer_finaliseur())

        self.assertNotIn("BLINK_HOME", relance)
        self.assertNotIn("BLINK_UPDATE_AUTO_HOME", relance)
        self.assertNotIn("BLINK_CONTROL_HOME", relance)
        with mock.patch.dict(os.environ, relance, clear=True), \
                mock.patch.object(maj.sys, "executable", str(self.installe / "blink2video.exe")):
            self.assertEqual(runtime.app_dir(), self.etat)
            self.assertEqual(runtime._dossier_controle(), self.etat)

    def test_relance_apres_un_lanceur_013_reprend_son_etat(self):
        """La version relancée ne garde rien des racines 0.13 : elle trouve
        seule le dossier d'état, et y reprend l'état de l'ancienne."""
        (self.donnees / "blink_auth.json").write_text("session 0.13", encoding="utf-8")
        (self.donnees / "Blink_Clips").mkdir()
        relance = self.relancer(self.environnement_lanceur_013())

        self.assertNotIn("BLINK_HOME", relance)
        self.assertNotIn("BLINK_CONTROL_HOME", relance)
        with mock.patch.dict(os.environ, relance, clear=True), \
                mock.patch.object(maj.sys, "executable", str(self.installe / "blink2video.exe")):
            runtime.preparer_etat()  # Ce que fait blink2video.py au démarrage.
            self.assertEqual(runtime.app_dir(), self.etat)
            self.assertEqual((self.etat / "blink_auth.json").read_text(encoding="utf-8"),
                             "session 0.13")
            self.assertEqual(runtime.dossier_sorties(), self.donnees)

    def test_relance_preserve_le_stockage_force_par_utilisateur(self):
        force = self.racine / "stockage-force"
        force.mkdir()
        os.environ["BLINK_HOME"] = str(force)
        relance = self.relancer(self.preparer_finaliseur())

        self.assertEqual(Path(relance["BLINK_HOME"]), force)
        self.assertNotIn("BLINK_UPDATE_AUTO_HOME", relance)
        with mock.patch.dict(os.environ, relance, clear=True):
            self.assertEqual(runtime._dossier_controle(), force)

    def test_ancien_installateur_sans_racine_de_controle_retrouve_instance(self):
        fiche = self.fiche(self.installe)
        # Les versions plus anciennes encore ne transmettaient que ce
        # BLINK_HOME synthétique, pas la racine des fiches.
        self.verifier_refus(dict(os.environ, BLINK_HOME=str(self.donnees)),
                            fiche, self.installe)

    def test_mise_a_jour_depuis_les_sources_retrouve_une_instance_013(self):
        """« update » depuis les sources ne transmet rien, et la version
        tirée par git n'a pas encore repris l'état : la fiche de l'instance
        0.13 en cours est encore à côté du programme, où il faut la trouver."""
        fiche = self.fiche(self.installe)
        with mock.patch.object(runtime, "frozen", return_value=False), \
                mock.patch.object(runtime, "_dossier_ancre", return_value=self.installe):
            self.verifier_refus(dict(os.environ), fiche, self.installe)

    def test_mise_a_jour_depuis_les_sources_apres_reprise(self):
        """Même chose une fois l'état repris : la fiche a suivi."""
        self.fiche(self.installe)
        with mock.patch.object(runtime, "frozen", return_value=False), \
                mock.patch.object(runtime, "_dossier_ancre", return_value=self.installe):
            runtime.preparer_etat()
            self.verifier_refus(dict(os.environ), self.etat / runtime.INSTANCES / "123.json",
                                self.etat)
        self.assertFalse((self.installe / runtime.INSTANCES / "123.json").exists())

    def test_ancien_installateur_refuse_racines_de_controle_ambigues(self):
        self.fiche(self.installe, 123)
        self.fiche(self.donnees, 456)
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
        # finaliser() cherche aussi les fiches dans le dossier d'état par
        # défaut : jamais le vrai, même lancé seul, sans BLINK_HOME.
        with tempfile.TemporaryDirectory(prefix="blink-maj-compositions-") as dossier, \
                mock.patch.object(runtime, "_dossier_etat_standard",
                                  return_value=Path(dossier) / "etat"), \
                mock.patch.object(runtime, "_ETATS_CREES", set()), \
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
