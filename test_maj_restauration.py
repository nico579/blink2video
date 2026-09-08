"""Échecs de copie/restauration sur une installation entièrement factice."""

import contextlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import maj
import runtime


class TestRestaurationMiseAJour(unittest.TestCase):
    def setUp(self):
        temporaire = tempfile.TemporaryDirectory(prefix="blink-maj-retour-")
        self.addCleanup(temporaire.cleanup)
        racine = Path(temporaire.name)
        self.installe, self.neuf = racine / "installe", racine / "neuf"
        for dossier in (self.installe, self.neuf):
            (dossier / "_internal").mkdir(parents=True)
        (self.installe / "blink2video.exe").write_bytes(b"original")
        (self.installe / "_internal" / "lib").write_bytes(b"lib-originale")
        (self.neuf / "blink2video.exe").write_bytes(b"neuf")
        (self.neuf / "_internal" / "lib").write_bytes(b"lib-neuve")
        (self.installe / "clip.mp4").write_bytes(b"clip conserve")
        self.marqueur = self.installe / maj.MARQUEUR_PERMUTATION
        self.poser = maj._poser

    def refuser_bibliotheques(self, source, cible):
        if source.name == "_internal":
            raise OSError("copie refusée")
        self.poser(source, cible)

    def test_echec_simple_restaure_et_autorise_nouvelle_tentative(self):
        with mock.patch.object(maj, "_poser", side_effect=self.refuser_bibliotheques):
            self.assertFalse(maj._permuter(self.neuf, self.installe))
        self.assertFalse(self.marqueur.exists())
        self.assertEqual((self.installe / "blink2video.exe").read_bytes(), b"original")
        self.assertEqual((self.installe / "_internal" / "lib").read_bytes(), b"lib-originale")
        self.assertTrue(maj._permuter(self.neuf, self.installe))
        self.assertEqual((self.installe / "blink2video.exe.ancien").read_bytes(), b"original")
        self.assertEqual((self.installe / "clip.mp4").read_bytes(), b"clip conserve")

    def provoquer_retour_incomplet(self):
        remplacer = os.replace

        def refuser_retour(source, cible):
            if Path(source).name == "blink2video.exe.ancien":
                raise PermissionError("restauration refusée")
            remplacer(source, cible)

        with mock.patch.object(maj, "_poser", side_effect=self.refuser_bibliotheques), \
                mock.patch.object(maj.os, "replace", side_effect=refuser_retour):
            with self.assertRaises(maj.RestaurationIncomplete):
                maj._permuter(self.neuf, self.installe)

    def test_restauration_incomplete_preserve_original_et_bloque_tous_reessais(self):
        self.provoquer_retour_incomplet()
        sauvegarde = self.installe / "blink2video.exe.ancien"
        self.assertEqual(sauvegarde.read_bytes(), b"original")
        self.assertTrue(self.marqueur.exists())
        for _ in range(3):
            with self.assertRaises(maj.RestaurationIncomplete):
                maj._permuter(self.neuf, self.installe)
            with self.assertRaises(maj.RestaurationIncomplete):
                maj._nettoyer(self.installe)
        self.assertEqual(sauvegarde.read_bytes(), b"original")
        self.assertEqual((self.installe / "clip.mp4").read_bytes(), b"clip conserve")

    def test_finaliseur_ne_reessaie_et_ne_relance_pas_apres_retour_incomplet(self):
        with mock.patch.object(runtime, "frozen", return_value=False), \
                mock.patch.object(runtime, "lire_instances", side_effect=[
                    [{"pid": 123, "verbes": [["serve"]]}], []]), \
                mock.patch.object(runtime, "lancer", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(maj, "_permuter", side_effect=maj.RestaurationIncomplete("arrêt sûr")) as permuter, \
                mock.patch.object(maj, "_relancer") as relancer, \
                mock.patch.object(maj.time, "sleep") as dormir:
            self.assertEqual(maj._finaliser(self.installe), 1)
        permuter.assert_called_once()
        dormir.assert_not_called()
        relancer.assert_not_called()

    def test_element_neuf_partiellement_copie_est_retire_au_retour(self):
        (self.neuf / "blink2video").write_bytes(b"autre executable")

        def poser_puis_echouer(source, cible):
            self.poser(source, cible)
            if source.name == "blink2video":
                raise OSError("copie partielle d'un élément nouveau")

        with mock.patch.object(maj, "_poser", side_effect=poser_puis_echouer):
            self.assertFalse(maj._permuter(self.neuf, self.installe))
        self.assertFalse((self.installe / "blink2video").exists())
        self.assertEqual((self.installe / "blink2video.exe").read_bytes(), b"original")
        self.assertFalse(self.marqueur.exists())

    def test_arret_brutal_preserve_la_marque_et_le_nettoyage_refuse(self):
        with mock.patch.object(maj, "_poser", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                maj._permuter(self.neuf, self.installe)
        self.assertTrue(self.marqueur.exists())
        self.assertEqual((self.installe / "blink2video.exe.ancien").read_bytes(), b"original")
        with self.assertRaises(maj.RestaurationIncomplete):
            maj._nettoyer(self.installe)

    def test_marqueur_refuse_ne_modifie_aucun_fichier(self):
        ouvrir = Path.open

        def refuser_marqueur(chemin, *args, **kwargs):
            if chemin == self.marqueur:
                raise PermissionError("marqueur refusé")
            return ouvrir(chemin, *args, **kwargs)

        with mock.patch.object(Path, "open", autospec=True, side_effect=refuser_marqueur):
            self.assertFalse(maj._permuter(self.neuf, self.installe))
        self.assertEqual((self.installe / "blink2video.exe").read_bytes(), b"original")
        self.assertFalse(self.marqueur.exists())
        self.assertFalse((self.installe / ".blink_maj-installation.lock").exists())

    def test_installateur_ne_nettoie_ni_ne_telecharge_apres_restauration_incomplete(self):
        self.marqueur.write_text("{}", encoding="utf-8")
        with mock.patch.object(runtime, "frozen", return_value=True), \
                mock.patch.object(runtime, "build_windows7", return_value=False), \
                mock.patch.object(maj.sys, "executable", str(self.installe / "blink2video.exe")), \
                mock.patch.object(maj, "disponible") as disponible, \
                mock.patch.object(maj, "_conclure_sans_relance"):
            self.assertEqual(maj.installer(), 1)
        disponible.assert_not_called()
        self.assertTrue(self.marqueur.exists())

    def test_nettoyage_en_cours_empeche_le_debut_d_une_permutation(self):
        marqueur_lu = threading.Event()
        continuer = threading.Event()
        erreurs = []
        exists = Path.exists

        def lire_marqueur(chemin):
            resultat = exists(chemin)
            if chemin == self.marqueur and threading.current_thread().name == "nettoyage-maj":
                marqueur_lu.set()
                if not continuer.wait(5):
                    raise RuntimeError("nettoyage simulé non libéré")
            return resultat

        def nettoyer():
            try:
                maj._nettoyer(self.installe)
            except Exception as erreur:
                erreurs.append(erreur)

        with mock.patch.object(Path, "exists", autospec=True, side_effect=lire_marqueur):
            fil = threading.Thread(target=nettoyer, name="nettoyage-maj")
            fil.start()
            try:
                self.assertTrue(marqueur_lu.wait(5))
                with self.assertRaises(maj.RestaurationIncomplete):
                    maj._permuter(self.neuf, self.installe)
                self.assertFalse(self.marqueur.exists())
                self.assertEqual((self.installe / "blink2video.exe").read_bytes(), b"original")
            finally:
                continuer.set()
                fil.join(5)
        self.assertFalse(fil.is_alive())
        self.assertEqual(erreurs, [])

    def test_nettoyage_concurrent_ne_purge_pas_la_sauvegarde_d_un_retour_incomplet(self):
        permutation_commencee = threading.Event()
        continuer = threading.Event()
        erreurs = []

        def poser(source, cible):
            if source.name == "blink2video.exe":
                permutation_commencee.set()
                if not continuer.wait(5):
                    raise RuntimeError("permutation simulée non libérée")
            self.poser(source, cible)

        remplacer = os.replace

        def refuser_retour(source, cible):
            if Path(source).name == "blink2video.exe.ancien":
                raise PermissionError("restauration refusée")
            remplacer(source, cible)

        def copie_ou_echec(source, cible):
            if source.name == "_internal":
                raise OSError("copie refusée")
            poser(source, cible)

        def permuter():
            try:
                maj._permuter(self.neuf, self.installe)
            except Exception as erreur:
                erreurs.append(erreur)

        sauvegarde = self.installe / "blink2video.exe.ancien"
        with mock.patch.object(maj, "_poser", side_effect=copie_ou_echec), \
                mock.patch.object(maj.os, "replace", side_effect=refuser_retour):
            fil = threading.Thread(target=permuter, name="permutation-maj")
            fil.start()
            try:
                self.assertTrue(permutation_commencee.wait(5))
                self.assertEqual(sauvegarde.read_bytes(), b"original")
                with self.assertRaises(maj.RestaurationIncomplete):
                    maj._nettoyer(self.installe)
                self.assertEqual(sauvegarde.read_bytes(), b"original")
            finally:
                continuer.set()
                fil.join(5)
        self.assertFalse(fil.is_alive())
        self.assertEqual(len(erreurs), 1)
        self.assertIsInstance(erreurs[0], maj.RestaurationIncomplete)
        self.assertTrue(self.marqueur.exists())
        with self.assertRaises(maj.RestaurationIncomplete):
            maj._nettoyer(self.installe)
        self.assertEqual(sauvegarde.read_bytes(), b"original")

    def test_reservation_refusee_ne_modifie_ni_programme_ni_sauvegarde(self):
        sauvegarde = self.installe / "blink2video.exe.ancien"
        sauvegarde.write_bytes(b"sauvegarde precedente")
        for erreur in (runtime.BusyError("mise à jour concurrente"),
                       PermissionError("verrou inaccessible")):
            @contextlib.contextmanager
            def refuser(*_args, **_kwargs):
                raise erreur
                yield

            for operation in (lambda: maj._permuter(self.neuf, self.installe),
                              lambda: maj._nettoyer(self.installe)):
                with self.subTest(erreur=type(erreur).__name__, operation=operation), \
                        mock.patch.object(runtime, "verrou", side_effect=refuser):
                    with self.assertRaises(maj.RestaurationIncomplete):
                        operation()
                    self.assertFalse(self.marqueur.exists())
                    self.assertEqual((self.installe / "blink2video.exe").read_bytes(), b"original")
                    self.assertEqual(sauvegarde.read_bytes(), b"sauvegarde precedente")

    def test_reservation_partagee_ignore_la_racine_de_stockage(self):
        autre_stockage = self.installe.parent / "donnees-ailleurs"
        autre_stockage.mkdir()
        with mock.patch.dict(os.environ, {"BLINK_HOME": str(autre_stockage)}), \
                maj._reservation_installation(self.installe):
            self.assertTrue((self.installe / ".blink_maj-installation.lock").exists())
            self.assertFalse((autre_stockage / ".blink_maj-installation.lock").exists())
            with self.assertRaises(maj.RestaurationIncomplete):
                maj._permuter(self.neuf, self.installe)
            with self.assertRaises(maj.RestaurationIncomplete):
                maj._nettoyer(self.installe)

    def test_erreur_du_corps_n_est_pas_requalifiee_en_echec_d_acquisition(self):
        erreur = PermissionError("échec du corps réservé")
        with mock.patch.object(maj, "_nettoyer_reserve", side_effect=erreur):
            with self.assertRaises(PermissionError) as recue:
                maj._nettoyer(self.installe)
        self.assertIs(recue.exception, erreur)
        self.assertFalse((self.installe / ".blink_maj-installation.lock").exists())

    def test_finaliseur_concurrent_ne_reessaie_et_ne_relance_pas(self):
        # Sur POSIX, identite_processus interroge ps via runtime.lancer.
        # Le faux lancement de stop ne doit pas lui fournir un Mock comme ID.
        with mock.patch.object(runtime, "identite_processus", return_value="processus-test"), \
                mock.patch.object(runtime, "processus_vivant", return_value=True), \
                maj._reservation_installation(self.installe), \
                mock.patch.object(runtime, "frozen", return_value=False), \
                mock.patch.object(runtime, "lire_instances", side_effect=[
                    [{"pid": 123, "verbes": [["serve"]]}], []]), \
                mock.patch.object(runtime, "lancer", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(maj, "_relancer") as relancer, \
                mock.patch.object(maj.time, "sleep") as dormir:
            self.assertEqual(maj._finaliser(self.installe), 1)
        dormir.assert_not_called()
        relancer.assert_not_called()
        self.assertEqual((self.installe / "blink2video.exe").read_bytes(), b"original")

    def test_installateur_concurrent_ne_nettoie_ni_ne_telecharge(self):
        sauvegarde = self.installe / "blink2video.exe.ancien"
        sauvegarde.write_bytes(b"sauvegarde precedente")
        with maj._reservation_installation(self.installe), \
                mock.patch.object(runtime, "frozen", return_value=True), \
                mock.patch.object(runtime, "build_windows7", return_value=False), \
                mock.patch.object(maj.sys, "executable", str(self.installe / "blink2video.exe")), \
                mock.patch.object(maj, "disponible") as disponible, \
                mock.patch.object(maj, "_conclure_sans_relance"):
            self.assertEqual(maj.installer(), 1)
        disponible.assert_not_called()
        self.assertEqual(sauvegarde.read_bytes(), b"sauvegarde precedente")


if __name__ == "__main__":
    unittest.main()
