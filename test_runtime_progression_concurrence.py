"""Publication/purge de progression : fixtures temporaires, aucun worker réel."""

import datetime as dt
import errno
import json
import multiprocessing
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import runtime


def _tenir_verrou_progression(cible, pret, liberer):
    """Processus de test isolé : seule sa fiche temporaire est accessible."""
    with runtime._verrou_travail(Path(cible)):
        pret.set()
        if not liberer.wait(10):
            raise RuntimeError("le test n'a pas libéré son verrou")


class ProgressionConcurrenceTests(unittest.TestCase):
    def setUp(self):
        dossier = tempfile.TemporaryDirectory(prefix="blink_progress_race_")
        self.addCleanup(dossier.cleanup)
        self.racine = Path(dossier.name)
        app_dir = mock.patch.object(runtime, "app_dir", return_value=self.racine)
        app_dir.start()
        self.addCleanup(app_dir.stop)
        self.cible = runtime._fichier_travail()

    def ancienne_fin(self):
        etat = {"quoi": "ancien", "fait": 1, "total": 1, "pid": os.getpid(),
                "termine": (dt.datetime.now().astimezone()
                            - dt.timedelta(seconds=30)).isoformat(),
                "visible_secondes": 1}
        self.cible.write_text(json.dumps(etat), encoding="utf-8")
        return etat

    def test_tick_neuf_apres_lecture_perimee_n_est_pas_supprime(self):
        self.ancienne_fin()
        lire = runtime._lire_fiche_travail
        premier = True

        def lire_puis_publier(cible):
            nonlocal premier
            etat = lire(cible)
            if premier:
                premier = False
                runtime.travail("nouveau", 0, 100, "phase.download_clips")
            return etat

        with mock.patch.object(runtime, "_lire_fiche_travail", side_effect=lire_puis_publier):
            runtime.travail_en_cours()
        self.assertTrue(self.cible.exists())
        self.assertEqual(runtime.travail_en_cours()["quoi"], "nouveau")

    def test_ancienne_fiche_inchangee_est_bien_purgee(self):
        self.ancienne_fin()
        self.assertEqual(runtime.travail_affichable(), {})
        self.assertFalse(self.cible.exists())
        # Un seul fichier stable pour les verrous, jamais une fiche affichée.
        self.assertEqual(list(self.racine.iterdir()),
                         [self.racine / ".blink_travail.lock"])

    def test_publication_attend_la_purge_d_un_autre_thread(self):
        ancien = self.ancienne_fin()
        demarre, termine = threading.Event(), threading.Event()
        resultat = []

        def publier():
            demarre.set()
            resultat.append(runtime._ecrire_fiche_travail(self.cible, {"quoi": "neuf"}))
            termine.set()

        fil = threading.Thread(target=publier)
        try:
            with runtime._verrou_travail(self.cible):
                fil.start()
                self.assertTrue(demarre.wait(2))
                self.assertFalse(termine.wait(0.03), "la publication doit partager le verrou")
                self.assertEqual(runtime._lire_fiche_travail(self.cible), ancien)
                self.cible.unlink()
        finally:
            if fil.ident is not None:
                fil.join(2)
        self.assertFalse(fil.is_alive())
        self.assertEqual(resultat, [True])
        self.assertEqual(runtime._lire_fiche_travail(self.cible), {"quoi": "neuf"})

    def test_verrou_interprocessus_purge_non_bloquante_et_reprise(self):
        self.ancienne_fin()
        contexte = multiprocessing.get_context("spawn")
        pret, liberer = contexte.Event(), contexte.Event()
        enfant = contexte.Process(target=_tenir_verrou_progression,
                                  args=(str(self.cible), pret, liberer))
        enfant.start()
        try:
            self.assertTrue(pret.wait(10), "le processus de test doit prendre le verrou")
            with self.assertRaises(runtime.BusyError):
                with runtime._verrou_travail(self.cible, attente=0):
                    pass
            self.assertEqual(runtime.travail_affichable(), {})
            self.assertTrue(self.cible.exists(), "purge reportée, sans attente de l'UI")
        finally:
            liberer.set()
            enfant.join(10)
        self.assertFalse(enfant.is_alive())
        self.assertEqual(enfant.exitcode, 0)
        runtime.travail("après libération", 1, 2)
        self.assertEqual(runtime.travail_en_cours()["quoi"], "après libération")

    def test_verrou_indisponible_ne_fait_pas_echouer_le_travail(self):
        ancien = self.ancienne_fin()
        for erreur in (PermissionError("antivirus"), runtime.BusyError("occupé")):
            with self.subTest(erreur=type(erreur).__name__), \
                    mock.patch.object(runtime, "_verrou_travail", side_effect=erreur):
                runtime.travail("neuf", 0, 2)
                runtime.fin_travail()
                self.assertEqual(runtime.travail_affichable(), {})
            self.assertEqual(runtime._lire_fiche_travail(self.cible), ancien)
        self.assertEqual(list(self.racine.glob("*.tmp")), [])

    def test_branche_posix_reessaie_sans_attente_longue_et_libere_sur_exception(self):
        fcntl = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8,
                                flock=mock.Mock(side_effect=[
                                    BlockingIOError(errno.EAGAIN, "occupé"), None, None]))
        with mock.patch.object(runtime.os, "name", "posix"), \
                mock.patch.dict(sys.modules, {"fcntl": fcntl}), \
                mock.patch.object(runtime.time, "sleep") as dormir:
            with self.assertRaisesRegex(ValueError, "travail interrompu"):
                with runtime._verrou_travail(self.cible):
                    raise ValueError("travail interrompu")
        self.assertEqual([appel.args[1] for appel in fcntl.flock.call_args_list],
                         [fcntl.LOCK_EX | fcntl.LOCK_NB,
                          fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_UN])
        dormir.assert_called_once_with(0.005)

    def test_polling_actif_ne_verrouille_pas_et_publication_ne_sonde_pas_les_processus(self):
        with mock.patch.object(runtime, "identite_processus",
                               side_effect=AssertionError("sondage superflu")):
            runtime.travail("actif", 0, 3)
        with mock.patch.object(runtime, "_verrou_travail",
                               side_effect=AssertionError("le polling actif reste sans verrou")):
            self.assertEqual(runtime.travail_en_cours()["quoi"], "actif")


if __name__ == "__main__":
    unittest.main()
