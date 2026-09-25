"""Dossiers de blink2video depuis 0.14 : l'état (réglages, session, fiches
des processus) dans le dossier standard de l'OS, ce qui est produit (clips,
vidéos) dans un dossier visible réglable depuis la page, et la reprise, une
seule fois, de l'état d'une version ≤ 0.13, rangé à côté du programme ou
dans la cible de son blink_home.txt (AUDIT-2026-08-13.md, section 28.35).

Aucun test ne touche au vrai dossier d'état : _dossier_etat_standard et
platformdirs.user_documents_dir sont redirigés vers un dossier temporaire."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import runtime


class BaseDossiers(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_dossiers_")
        self.racine = Path(self.temporaire.name).resolve()
        self.ancre = self.racine / "programme"
        self.ancre.mkdir()
        self.etat = self.racine / "etat"
        self.documents = self.racine / "Documents"
        self.defaut_sorties = self.documents / runtime.ENTREE
        for correctif in (
                mock.patch.object(runtime, "_dossier_ancre", return_value=self.ancre),
                mock.patch.object(runtime, "_dossier_etat_standard", return_value=self.etat),
                mock.patch("platformdirs.user_documents_dir",
                           return_value=str(self.documents)),
                mock.patch.object(runtime, "_ETATS_CREES", set()),
                mock.patch.dict(os.environ, {}, clear=False)):
            correctif.start()
            self.addCleanup(correctif.stop)
        for nom in ("BLINK_HOME", "BLINK_CONTROL_HOME", "BLINK_UPDATE_AUTO_HOME"):
            os.environ.pop(nom, None)
        self.addCleanup(self.temporaire.cleanup)

    def reglages(self) -> dict:
        return json.loads((self.etat / runtime.REGLAGES).read_text(encoding="utf-8"))


class TestsEmplacements(BaseDossiers):
    def test_etat_dans_le_dossier_standard(self):
        self.assertEqual(runtime.app_dir(), self.etat)
        self.assertTrue(self.etat.is_dir())

    def test_sorties_par_defaut_dans_documents(self):
        self.assertEqual(runtime.dossier_sorties(), self.defaut_sorties)
        self.assertEqual(runtime.lire_dossier_stockage(), str(self.defaut_sorties))

    def test_blink_home_impose_l_etat_et_les_sorties(self):
        force = self.racine / "force"
        force.mkdir()
        os.environ["BLINK_HOME"] = str(force)
        self.assertEqual(runtime.app_dir(), force)
        self.assertEqual(runtime.dossier_sorties(), force)
        self.assertEqual(runtime._dossier_controle(), force)
        # Pas de reprise ni de dossier standard créé pour autant.
        self.assertFalse(self.etat.exists())

    def test_reglage_des_sorties_l_emporte_sur_blink_home(self):
        force = self.racine / "force"
        force.mkdir()
        os.environ["BLINK_HOME"] = str(force)
        cible = self.racine / "videos"
        runtime.ecrire_dossier_stockage(str(cible))
        self.assertEqual(runtime.dossier_sorties(), cible)
        self.assertEqual(runtime.app_dir(), force)

    def test_controle_avec_l_etat(self):
        self.assertEqual(runtime._dossier_controle(), self.etat)

    def test_app_dir_depuis_sans_pointeur_rend_l_ancre_fournie(self):
        # maj.py l'appelle avec le dossier d'installation réel, pas celui que
        # _dossier_ancre() calculerait pour CE processus (dossier temporaire).
        autre_ancre = self.racine / "installation_reelle"
        autre_ancre.mkdir()
        self.assertEqual(runtime.app_dir_depuis(autre_ancre), autre_ancre)

    def test_app_dir_depuis_suit_le_pointeur_de_l_ancre_fournie(self):
        # Bug corrigé le 27 août 2026 (signalé sur Reddit) : le dossier de
        # données revenait à celui de l'exécutable après une mise à jour.
        autre_ancre = self.racine / "installation_reelle"
        autre_ancre.mkdir()
        cible = self.racine / "stockage_redirige"
        (autre_ancre / runtime.POINTEUR_STOCKAGE).write_text(str(cible), encoding="utf-8")
        self.assertEqual(runtime.app_dir_depuis(autre_ancre), cible.resolve())


class TestsReglageDesSorties(BaseDossiers):
    def test_changer_les_sorties(self):
        cible = self.racine / "videos"
        runtime.ecrire_dossier_stockage(str(cible))
        self.assertTrue(cible.is_dir())
        self.assertEqual(runtime.dossier_sorties(), cible)
        self.assertEqual(runtime.lire_dossier_stockage(), str(cible))
        self.assertEqual(runtime.app_dir(), self.etat)

    def test_reglage_vide_revient_au_defaut(self):
        runtime.ecrire_dossier_stockage(str(self.racine / "videos"))
        runtime.ecrire_dossier_stockage("")
        self.assertEqual(runtime.dossier_sorties(), self.defaut_sorties)
        self.assertEqual(self.reglages()["dossier_sorties"], "")

    def test_changer_les_sorties_ne_deplace_ni_session_ni_clips(self):
        """Avant 0.14, changer de dossier déplaçait aussi l'état : session et
        réglages devaient y être recopiés (revue du 27/08, « je perds mon
        authentification »). Ils ne bougent plus du tout."""
        runtime.app_dir()
        (self.etat / "blink_auth.json").write_text('{"token": "abc"}', encoding="utf-8")
        ancien = self.racine / "anciennes_videos"
        clip = ancien / "Blink_Clips" / "camera" / "clip.mp4"
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b"clip")
        runtime.ecrire_dossier_stockage(str(ancien))
        cible = self.racine / "videos"
        runtime.ecrire_dossier_stockage(str(cible))
        self.assertEqual((self.etat / "blink_auth.json").read_text(encoding="utf-8"),
                         '{"token": "abc"}')
        self.assertEqual(clip.read_bytes(), b"clip")
        self.assertEqual(list(cible.iterdir()), [])

    def test_les_autres_reglages_gardent_le_dossier_choisi(self):
        cible = self.racine / "videos"
        runtime.ecrire_dossier_stockage(str(cible))
        runtime.ecrire_reglages(5, 2, 8765, False, "Europe/Paris",
                                True, True, True, True, "webrtc")
        self.assertEqual(runtime.dossier_sorties(), cible)

    def test_reglages_fournis_ecrits_avec_le_dossier(self):
        reglages = {cle: valeur for cle, valeur in runtime.lire_reglages().items()
                    if cle != "dossier_sorties"}
        reglages["port"] = 9999
        cible = self.racine / "videos"
        runtime.ecrire_dossier_stockage(str(cible), reglages=reglages)
        self.assertEqual(self.reglages()["port"], 9999)
        self.assertEqual(self.reglages()["dossier_sorties"], str(cible))

    def test_destination_inutilisable_refusee_sans_rien_enregistrer(self):
        runtime.ecrire_dossier_stockage(str(self.racine / "videos"))
        avant = (self.etat / runtime.REGLAGES).read_bytes()
        fichier = self.racine / "un_fichier"
        fichier.write_text("pas un dossier", encoding="utf-8")
        with self.assertRaises(OSError):
            runtime.ecrire_dossier_stockage(str(fichier / "sous_dossier"))
        self.assertEqual((self.etat / runtime.REGLAGES).read_bytes(), avant)

    def test_echec_marqueur_restaure_les_reglages_exacts(self):
        runtime.app_dir()
        contenu = b'{ "port": 9999 }\r\n'
        (self.etat / runtime.REGLAGES).write_bytes(contenu)
        with mock.patch.object(runtime, "marquer_configuration_initiale",
                               side_effect=OSError("marqueur refusé")):
            with self.assertRaisesRegex(OSError, "marqueur refusé"):
                runtime.ecrire_dossier_stockage(str(self.racine / "videos"),
                                                configuration_initiale=True)
        self.assertEqual((self.etat / runtime.REGLAGES).read_bytes(), contenu)
        self.assertEqual(list(self.etat.glob(".*.tmp")), [])

    def test_echec_marqueur_sans_reglages_prealables_les_retire(self):
        with mock.patch.object(runtime, "marquer_configuration_initiale",
                               side_effect=OSError("marqueur refusé")):
            with self.assertRaisesRegex(OSError, "marqueur refusé"):
                runtime.ecrire_dossier_stockage(str(self.racine / "videos"),
                                                configuration_initiale=True)
        self.assertFalse((self.etat / runtime.REGLAGES).exists())


class TestsConfigurationInitiale(BaseDossiers):
    def test_installation_neuve_reste_en_attente_jusqu_a_validation(self):
        self.assertTrue(runtime.configuration_initiale_requise())
        self.assertTrue((self.etat / runtime.MARQUEUR_CONFIGURATION_EN_ATTENTE).is_file())
        # La connexion créée pendant ce parcours ne doit pas être prise pour
        # une preuve d'installation historique au lancement suivant.
        (self.etat / "blink_auth.json").write_text("{}", encoding="utf-8")
        self.assertTrue(runtime.configuration_initiale_requise())
        self.assertFalse(runtime.configuration_initiale_effectuee())

    def test_validation_initiale_remplace_l_attente_par_le_marqueur_definitif(self):
        self.assertTrue(runtime.configuration_initiale_requise())
        runtime.marquer_configuration_initiale()
        self.assertTrue(runtime.configuration_initiale_effectuee())
        self.assertFalse((self.etat / runtime.MARQUEUR_CONFIGURATION_EN_ATTENTE).exists())
        self.assertFalse(runtime.configuration_initiale_requise())

    def test_session_existante_sans_marqueur_n_impose_pas_le_panneau(self):
        runtime.app_dir()
        (self.etat / "blink_auth.json").write_text("{}", encoding="utf-8")
        self.assertFalse(runtime.configuration_initiale_requise())
        self.assertTrue(runtime.configuration_initiale_effectuee())

    def test_clips_d_une_ancienne_version_sans_session(self):
        clips = self.ancre / "Blink_Clips" / "camera"
        clips.mkdir(parents=True)
        (clips / "clip.mp4").write_bytes(b"ancien clip")
        self.assertFalse(runtime.configuration_initiale_requise())
        self.assertTrue(runtime.configuration_initiale_effectuee())

    def test_changer_vers_un_dossier_vide_ne_redevient_pas_une_installation_neuve(self):
        runtime.configuration_initiale_requise()
        runtime.marquer_configuration_initiale()
        cible = self.racine / "stockage_vide"
        runtime.ecrire_dossier_stockage(str(cible))
        self.assertEqual(runtime.dossier_sorties(), cible)
        self.assertFalse(runtime.configuration_initiale_requise())
        self.assertFalse((cible / runtime.MARQUEUR_CONFIGURATION_INITIALE).exists())

    def test_changer_les_sorties_ne_perd_pas_instance_ni_demande_arret(self):
        fiches = self.etat / runtime.INSTANCES
        fiches.mkdir(parents=True)
        (fiches / "123.json").write_text(json.dumps({
            "pid": 123, "depuis": "maintenant", "verbes": [["serve"]], "enfants": [],
        }), encoding="utf-8")
        cible = self.racine / "videos"
        runtime.ecrire_dossier_stockage(str(cible))
        with mock.patch.object(runtime, "processus_correspond", return_value=True):
            instances = runtime.lire_instances()
        self.assertEqual([instance["pid"] for instance in instances], [123])
        runtime.demander_arret()
        self.assertTrue(runtime.arret_demande())
        self.assertTrue((self.etat / runtime.ARRET_DEMANDE).exists())
        self.assertFalse((cible / runtime.ARRET_DEMANDE).exists())


class TestsRepriseDeLEtat(BaseDossiers):
    def installation_013(self, donnees: Path) -> dict:
        """État et clips tels que les laissait une version ≤ 0.13."""
        donnees.mkdir(parents=True, exist_ok=True)
        contenus = {nom: f"contenu de {nom}\r\n".encode("utf-8")
                    for nom in runtime.ETAT_HISTORIQUE}
        contenus[runtime.REGLAGES] = b'{ "port": 9999 }\r\n'
        for nom, contenu in contenus.items():
            (donnees / nom).write_bytes(contenu)
        clip = donnees / "Blink_Clips" / "camera" / "clip.mp4"
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b"clip")
        (self.ancre / runtime.MARQUEUR_CONFIGURATION_INITIALE).write_text(
            "0.13.7", encoding="utf-8")
        return contenus

    def test_app_dir_seul_ne_reprend_rien(self):
        """Un simple calcul de chemin (un test, un outil) ne copie aucun état
        et ne déplace aucune fiche : seul preparer_etat() le fait."""
        self.installation_013(self.ancre)
        fiches = self.ancre / runtime.INSTANCES
        fiches.mkdir()
        (fiches / "123.json").write_text("{}", encoding="utf-8")
        self.assertEqual(runtime.app_dir(), self.etat)
        self.assertEqual(list(self.etat.iterdir()), [])
        self.assertTrue((fiches / "123.json").exists())

    def test_blink_home_sans_reprise(self):
        self.installation_013(self.ancre)
        force = self.racine / "force"
        force.mkdir()
        os.environ["BLINK_HOME"] = str(force)
        runtime.preparer_etat()
        self.assertEqual(list(force.iterdir()), [])
        self.assertFalse(self.etat.exists())

    def test_reprend_l_etat_et_laisse_les_clips_en_place(self):
        contenus = self.installation_013(self.ancre)
        runtime.preparer_etat()
        for nom, contenu in contenus.items():
            if nom == runtime.REGLAGES:
                continue
            self.assertEqual((self.etat / nom).read_bytes(), contenu, nom)
            self.assertEqual((self.ancre / nom).read_bytes(), contenu, nom)
        reglages = self.reglages()
        self.assertEqual(reglages["port"], 9999)
        self.assertEqual(reglages["dossier_sorties"], str(self.ancre))
        self.assertEqual(runtime.dossier_sorties(), self.ancre)
        self.assertEqual((self.ancre / "Blink_Clips" / "camera" / "clip.mp4").read_bytes(),
                         b"clip")
        self.assertFalse((self.etat / "Blink_Clips").exists())
        self.assertTrue(runtime.configuration_initiale_effectuee())
        rapport = json.loads((self.etat / runtime.MARQUEUR_MIGRATION).read_text(encoding="utf-8"))
        self.assertEqual(rapport["depuis"], str(self.ancre))
        self.assertEqual(rapport["dossier_sorties"], str(self.ancre))
        self.assertIn("blink_auth.json", rapport["repris"])
        self.assertEqual(list(self.etat.glob(".*.tmp")), [])

    def test_suit_le_pointeur_d_une_version_013(self):
        donnees = self.racine / "donnees_redirigees"
        contenus = self.installation_013(donnees)
        (self.ancre / runtime.POINTEUR_STOCKAGE).write_text(str(donnees), encoding="utf-8")
        runtime.preparer_etat()
        self.assertEqual((self.etat / "blink_auth.json").read_bytes(),
                         contenus["blink_auth.json"])
        self.assertEqual(runtime.dossier_sorties(), donnees)
        # Les marqueurs vivaient à côté du programme, pas avec les données.
        self.assertTrue(runtime.configuration_initiale_effectuee())

    def test_n_ecrase_rien_de_ce_que_l_etat_contient(self):
        self.installation_013(self.ancre)
        self.etat.mkdir()
        (self.etat / "blink_auth.json").write_text("session récente", encoding="utf-8")
        runtime.preparer_etat()
        self.assertEqual((self.etat / "blink_auth.json").read_text(encoding="utf-8"),
                         "session récente")

    def test_une_seule_fois(self):
        self.installation_013(self.ancre)
        runtime.preparer_etat()
        (self.etat / runtime.LANGUE).unlink()
        runtime.preparer_etat()
        self.assertFalse((self.etat / runtime.LANGUE).exists())

    def test_fiches_deplacees_pour_que_stop_trouve_l_instance(self):
        """Une instance 0.13 encore en cours doit rester visible de stop."""
        ancienne = self.ancre / runtime.INSTANCES
        ancienne.mkdir()
        (ancienne / "123.json").write_text(json.dumps({
            "pid": 123, "depuis": "hier", "verbes": [["start"]], "enfants": [],
        }), encoding="utf-8")
        runtime.preparer_etat()
        with mock.patch.object(runtime, "processus_correspond", return_value=True):
            instances = runtime.lire_instances()
        self.assertEqual([instance["pid"] for instance in instances], [123])
        self.assertFalse((ancienne / "123.json").exists())

    def test_installation_neuve_ne_clot_pas_la_reprise(self):
        """Le finaliseur d'une mise à jour tourne depuis un dossier temporaire
        vide : il ne doit pas empêcher la version installée de reprendre
        l'état ensuite."""
        runtime.preparer_etat()
        self.assertFalse((self.etat / runtime.MARQUEUR_MIGRATION).exists())
        self.installation_013(self.ancre)
        runtime.preparer_etat()
        self.assertTrue((self.etat / "blink_auth.json").is_file())
        self.assertTrue((self.etat / runtime.MARQUEUR_MIGRATION).is_file())

    def test_etat_d_essai_n_empeche_pas_la_reprise(self):
        """Des réglages écrits par un essai (test lancé sans isolation, outil)
        sans rien à reprendre ne posent pas de marqueur : la vraie reprise
        aura lieu quand l'ancien état sera là."""
        (runtime.app_dir() / runtime.REGLAGES).write_text("{}", encoding="utf-8")
        runtime.preparer_etat()
        self.assertFalse((self.etat / runtime.MARQUEUR_MIGRATION).exists())
        self.installation_013(self.ancre)
        runtime.preparer_etat()
        self.assertTrue((self.etat / "blink_auth.json").is_file())
        self.assertTrue((self.etat / runtime.MARQUEUR_MIGRATION).is_file())

    def test_reglages_illisibles_jamais_ecrases(self):
        self.installation_013(self.ancre)
        self.etat.mkdir()
        (self.etat / runtime.REGLAGES).write_bytes(b"{ illisible")
        runtime.preparer_etat()
        self.assertEqual((self.etat / runtime.REGLAGES).read_bytes(), b"{ illisible")

    def test_echec_de_reprise_ne_bloque_pas_et_sera_retente(self):
        self.installation_013(self.ancre)
        with mock.patch.object(runtime.shutil, "copy2", side_effect=OSError("disque plein")):
            runtime.preparer_etat()
        self.assertFalse((self.etat / runtime.MARQUEUR_MIGRATION).exists())
        self.assertIn("disque plein",
                      (self.etat / "migration.log").read_text(encoding="utf-8"))
        runtime.preparer_etat()
        self.assertTrue((self.etat / "blink_auth.json").is_file())
        self.assertTrue((self.etat / runtime.MARQUEUR_MIGRATION).is_file())


class TestsSansDossierRedirige(unittest.TestCase):
    """Fonctions pures : aucun dossier n'est créé ici."""

    def test_repli_sans_platformdirs_donne_le_meme_dossier(self):
        # Avant bootstrap(), runtime calcule seul le dossier d'état. Il doit
        # tomber au même endroit que platformdirs, sur chaque système de la CI.
        attendu = runtime._dossier_etat_standard()
        with mock.patch.dict(sys.modules, {"platformdirs": None}):
            self.assertEqual(runtime._dossier_etat_standard(), attendu)

    def test_noms_repris_et_produits_concordent_avec_les_modules(self):
        with tempfile.TemporaryDirectory(prefix="blink_noms_") as dossier, \
                mock.patch.dict(os.environ, {"BLINK_HOME": dossier,
                                             "BLINK_BOOTSTRAP": "none"}):
            import blink_auth
            import blink_cli
            import maj
            import merge_daily as md
            import serve
            import watch
        attendus = {blink_auth.CONFIG.name, runtime.PASSAGES.name,
                    watch.WATCH_STATE.name, blink_cli.MARQUEUR_RACCOURCI,
                    maj.CACHE.name, runtime.REGLAGES, runtime.LANGUE,
                    runtime.JETON_WEBHOOK, runtime.SUPPRESSION_AUTO}
        self.assertEqual(set(runtime.ETAT_HISTORIQUE), attendus)
        produits = {md.DEFAULT_INPUT.name, md.DEFAULT_OUTPUT.name, md.DEFAULT_WEEKLY.name,
                    md.DEFAULT_MONTHLY.name, md.DEFAULT_NORMALIZED.name,
                    md.DEFAULT_EXCLUDED.name, serve.DOSSIER_DIRECT.name,
                    serve.DOSSIER_SNAPSHOTS.name}
        self.assertEqual(set(runtime.DOSSIERS_SORTIES), produits)


if __name__ == "__main__":
    unittest.main()
