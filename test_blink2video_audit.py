"""Tests isolés de caractérisation issus de l'audit du 13 août 2026.

Cette suite n'utilise ni compte Blink, ni réseau Internet, ni données réelles.
Les défauts déjà confirmés sont marqués ``expectedFailure`` : ils restent
visibles dans le rapport unittest, mais n'empêchent pas l'installation du filet
de sécurité. Dès qu'un correctif les fait réussir, unittest signale un
``unexpected success`` et impose de retirer le marqueur dans le même changement.

Exécution :

    python -B -m unittest -v test_blink2video_audit.py
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import io
import json
import multiprocessing
import os
import queue
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parent
os.environ["BLINK_BOOTSTRAP"] = "none"

import blink2video as b2v  # noqa: E402 - bootstrap neutralisé avant import
import runtime  # noqa: E402 - bootstrap neutralisé avant import


class FauxClip:
    def __init__(self, identifiant=1, nom="jardin", instant=None, network_id=7):
        self.id = identifiant
        self.name = nom
        self.created_at = instant or dt.datetime(2026, 8, 13, 12, tzinfo=dt.timezone.utc)
        self.network_id = network_id
        self.size = 1


class FauxSync:
    def __init__(self, sync_id=10):
        self.sync_id = sync_id


class FauxReponse:
    def __init__(self, statut: int, contenu: bytes):
        self.status = statut
        self._contenu = contenu

    async def read(self):
        return self._contenu


class FauxBlinkHTTP:
    def __init__(self, reponse):
        self.reponse = reponse

    async def do_http_get(self, _adresse):
        return self.reponse


def _worker_course_verrou(home: str, depart, avant_ecriture, liberation, resultats):
    """Force deux implémentations check-then-write à observer un verrou absent."""
    os.environ["BLINK_HOME"] = home
    os.environ["BLINK_BOOTSTRAP"] = "none"
    import pathlib
    import runtime as runtime_enfant

    original = pathlib.Path.write_text

    def ecriture_retardee(chemin, *args, **kwargs):
        if chemin.name == ".blink_course.lock":
            avant_ecriture.wait(timeout=10)
        return original(chemin, *args, **kwargs)

    pathlib.Path.write_text = ecriture_retardee
    try:
        depart.wait(timeout=10)
        try:
            with runtime_enfant.verrou("course", f"worker-{os.getpid()}"):
                resultats.put(("entered", os.getpid()))
                liberation.wait(timeout=10)
        except runtime_enfant.BusyError:
            resultats.put(("busy", os.getpid()))
        except Exception as erreur:  # Le parent doit voir toute panne du worker.
            resultats.put(("error", type(erreur).__name__, str(erreur)))
    finally:
        pathlib.Path.write_text = original


class BacASable(unittest.TestCase):
    def setUp(self):
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_audit_")
        self.racine = Path(self.temporaire.name)
        self.home = self.racine / "home"
        self.cwd = self.racine / "cwd"
        self.home.mkdir()
        self.cwd.mkdir()
        self.ancien_cwd = Path.cwd()
        self.ancien_home = os.environ.get("BLINK_HOME")
        os.environ["BLINK_HOME"] = str(self.home)
        os.chdir(self.cwd)

    def tearDown(self):
        os.chdir(self.ancien_cwd)
        if self.ancien_home is None:
            os.environ.pop("BLINK_HOME", None)
        else:
            os.environ["BLINK_HOME"] = self.ancien_home
        self.temporaire.cleanup()


class TestsGardes(BacASable):
    def test_G01_bootstrap_est_desactive(self):
        self.assertEqual(os.environ.get("BLINK_BOOTSTRAP"), "none")

    def test_G02_session_nominale_efface_le_mot_de_passe(self):
        session = self.home / "blink_auth.json"
        session.write_text(json.dumps({
            "refresh_token": "jeton-factice",
            "username": "personne@example.invalid",
            "password": "ne-doit-pas-sortir",
        }), encoding="utf-8")
        with mock.patch.object(b2v, "CONFIG", session):
            chargee = b2v.load_saved_session()
        self.assertEqual(chargee["password"], "")
        self.assertEqual(chargee["refresh_token"], "jeton-factice")

    def test_G03_etat_absent_est_un_registre_vide(self):
        self.assertEqual(
            b2v.load_download_state(self.home / "clips"),
            {"version": 1, "clips": {}},
        )

    def test_G04_boucle_positive_formes_separee_et_egale(self):
        parseur = argparse.ArgumentParser()
        runtime.ajouter_boucle(parseur)
        self.assertEqual(parseur.parse_args(["--loop", "3"]).loop, 3)
        self.assertEqual(parseur.parse_args(["--loop=3"]).loop, 3)

    def test_G05_rapprochement_respecte_la_tolerance(self):
        local = FauxClip(1, instant=dt.datetime(2026, 8, 13, 12, 0, 0,
                                                tzinfo=dt.timezone.utc))
        proche = FauxClip(2, instant=dt.datetime(2026, 8, 13, 12, 0, 2,
                                                 tzinfo=dt.timezone.utc))
        loin = FauxClip(3, instant=dt.datetime(2026, 8, 13, 12, 0, 3,
                                               tzinfo=dt.timezone.utc))
        inedits, doublons = b2v.rapprocher([local], [proche, loin], tolerance=2)
        self.assertEqual([clip.id for clip in doublons], [2])
        self.assertEqual([clip.id for clip in inedits], [3])

    def test_G06_partiel_est_nettoye_apres_exception(self):
        cible = self.home / "clips" / "cible.mp4"

        class ClipEnErreur(FauxClip):
            async def prepare_download(self, _blink):
                cible.with_suffix(".mp4.part").parent.mkdir(parents=True, exist_ok=True)
                cible.with_suffix(".mp4.part").write_bytes(b"partiel")
                raise RuntimeError("panne simulée")

        with self.assertRaises(RuntimeError):
            asyncio.run(b2v.download_clip(object(), ClipEnErreur(), cible, False))
        self.assertFalse(cible.with_suffix(".mp4.part").exists())

    def test_G07_unexpected_success_met_la_suite_en_echec(self):
        """Le retrait oublié d'un expectedFailure doit rendre la CI rouge."""
        class CorrectionSimulee(unittest.TestCase):
            @unittest.expectedFailure
            def runTest(self):
                pass

        resultat = unittest.TestResult()
        CorrectionSimulee().run(resultat)
        self.assertEqual(len(resultat.unexpectedSuccesses), 1)
        self.assertFalse(resultat.wasSuccessful())


class TestsDefautsSynchrones(BacASable):
    @unittest.expectedFailure
    def test_I01_BLINK_HOME_est_la_racine_de_config_et_sortie(self):
        """I-01 : CONFIG et OUTPUT ne doivent jamais dépendre du CWD."""
        code = (
            "import json, blink2video as b; "
            "print(json.dumps([str(b.CONFIG.resolve()), str(b.OUTPUT.resolve())]))"
        )
        env = dict(os.environ, BLINK_HOME=str(self.home), BLINK_BOOTSTRAP="none",
                   PYTHONPATH=str(ROOT))
        resultat = subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=self.cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", check=False,
            timeout=15,
        )
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        config, output = [Path(p) for p in json.loads(resultat.stdout)]
        self.assertEqual(config.parent, self.home.resolve())
        self.assertEqual(output.parent, self.home.resolve())

    @unittest.expectedFailure
    def test_I10_session_json_tableau_est_refusee_sans_exception(self):
        """I-10 : une racine JSON valide mais non objet doit être tolérée."""
        session = self.home / "blink_auth.json"
        session.write_text("[]", encoding="utf-8")
        with mock.patch.object(b2v, "CONFIG", session):
            self.assertIsNone(b2v.load_saved_session())

    @unittest.expectedFailure
    def test_I10_registre_json_tableau_est_refuse_sans_exception(self):
        """I-10 : le chargeur d'état valide le type avant .get()."""
        sortie = self.home / "clips"
        sortie.mkdir()
        (sortie / b2v.STATE_FILENAME).write_text("[]", encoding="utf-8")
        self.assertEqual(b2v.load_download_state(sortie),
                         {"version": 1, "clips": {}})

    @unittest.expectedFailure
    def test_I11_valeur_de_camera_egale_a_un_verbe_reste_une_valeur(self):
        """I-11 : `download --camera watch` forme une seule commande."""
        self.assertEqual(
            runtime.decouper_verbes(["download", "--camera", "watch"]),
            [["download", "--camera", "watch"]],
        )

    @unittest.expectedFailure
    def test_I12_boucle_nulle_est_rejetee(self):
        """I-12 : zéro ne doit pas être accepté comme cadence."""
        parseur = argparse.ArgumentParser()
        runtime.ajouter_boucle(parseur)
        with self.assertRaises(SystemExit):
            parseur.parse_args(["--loop", "0"])

    @unittest.expectedFailure
    def test_I12_boucle_negative_est_rejetee(self):
        """I-12 : une cadence négative créerait une boucle serrée."""
        parseur = argparse.ArgumentParser()
        runtime.ajouter_boucle(parseur)
        with self.assertRaises(SystemExit):
            parseur.parse_args(["--loop", "-1"])

    @unittest.expectedFailure
    def test_I13_parent_mort_ne_fait_pas_oublier_un_enfant_vivant(self):
        """I-13 : une fiche reste utile tant qu'un de ses membres vit."""
        dossier = self.home / runtime.INSTANCES
        dossier.mkdir()
        fiche = dossier / "111.json"
        fiche.write_text(json.dumps({
            "pid": 111, "depuis": "2026-08-13T12:00:00+02:00",
            "verbes": [["serve"]], "enfants": [222],
        }), encoding="utf-8")
        with mock.patch.object(runtime, "processus_vivant",
                               side_effect=lambda pid: int(pid) == 222):
            instances = runtime.lire_instances()
        self.assertEqual(len(instances), 1)
        self.assertTrue(fiche.exists())

    @unittest.expectedFailure
    def test_I14_echec_arret_conserve_la_fiche(self):
        """I-14 : stop ne doit pas perdre la seule piste d'un survivant."""
        fiche = self.home / "instance.json"
        fiche.write_text("{}", encoding="utf-8")
        instance = {"pid": 111, "depuis": "maintenant", "verbes": [["serve"]],
                    "enfants": [222], "fiche": fiche}
        with mock.patch.object(runtime, "lire_instances", return_value=[instance]), \
             mock.patch.object(runtime, "arreter_processus"), \
             mock.patch.object(runtime, "processus_vivant", return_value=True), \
             contextlib.redirect_stdout(io.StringIO()):
            code = b2v.arreter([])
        self.assertEqual(code, 1)
        self.assertTrue(fiche.exists())

    @unittest.expectedFailure
    def test_E01_sans_argument_selectionne_start(self):
        """E-01 : l'absence d'argument doit emprunter le parcours start."""
        with mock.patch.object(sys, "argv", ["blink2video"]), \
             contextlib.redirect_stdout(io.StringIO()):
            arguments = b2v.parse_args()
        self.assertEqual(arguments.command, "start")

    @unittest.expectedFailure
    def test_B03_renumerotation_utilise_le_chemin_memorise(self):
        """B-03 : un ID distant nouveau ne retélécharge pas le même événement."""
        sortie = self.home / "clips"
        sync = FauxSync()
        ancien = FauxClip(42)
        nouveau = FauxClip(99)
        ancienne_cible = b2v.target_path(sortie, ancien)
        ancienne_cible.parent.mkdir(parents=True)
        ancienne_cible.write_bytes(b"video-originale")
        etat = {"version": 1, "clips": {}}
        b2v.remember_download(etat, sync, "Test", ancien, sortie, ancienne_cible)
        nouvelle_cible = b2v.target_path(sortie, nouveau)
        self.assertEqual(b2v.state_key(sync, ancien), b2v.state_key(sync, nouveau))
        self.assertTrue(b2v.is_downloaded(etat, sync, nouveau, nouvelle_cible))

    @unittest.expectedFailure
    def test_B04_fusion_perimee_preserve_une_exclusion_plus_recente(self):
        """B-04 : une copie périmée ne peut retirer une tombstone."""
        sortie = self.home / "clips"
        sortie.mkdir()
        fichier = sortie / b2v.STATE_FILENAME
        fichier.write_text(json.dumps({
            "version": 1,
            "clips": {"commun": {"path": "x.mp4", "excluded": True}},
        }), encoding="utf-8")
        perime = {"version": 1, "clips": {
            "commun": {"path": "x.mp4"},
            "nouveau": {"path": "y.mp4"},
        }}
        b2v._ecrire_registre(fichier, perime)
        fusion = json.loads(fichier.read_text(encoding="utf-8"))
        self.assertIs(fusion["clips"]["commun"].get("excluded"), True)

    @unittest.expectedFailure
    def test_B05_un_verrou_vivant_ne_perime_pas_sur_le_seul_ttl(self):
        """B-05 : le TTL ne permet pas de voler un propriétaire vivant."""
        verrou = self.home / ".blink_ttl.lock"
        verrou.write_text(json.dumps({
            "owner": "travail-long", "pid": os.getpid(), "at": time.time() - 120,
        }), encoding="utf-8")
        with self.assertRaises(runtime.BusyError):
            with runtime.verrou("ttl", "challenger", stale_after=60):
                pass

    @unittest.expectedFailure
    def test_B05_acquisition_concurrente_n_a_qu_un_gagnant(self):
        """B-05 : deux check-then-write synchronisés ne doivent pas entrer."""
        contexte = multiprocessing.get_context("spawn")
        depart = contexte.Barrier(2)
        avant_ecriture = contexte.Barrier(2)
        liberation = contexte.Event()
        resultats = contexte.Queue()
        processus = [
            contexte.Process(
                target=_worker_course_verrou,
                args=(str(self.home), depart, avant_ecriture, liberation, resultats),
            ) for _ in range(2)
        ]
        for processus_enfant in processus:
            processus_enfant.start()
        messages = []
        try:
            for _ in processus:
                messages.append(resultats.get(timeout=15))
        except queue.Empty:
            self.fail(f"workers incomplets : {messages}")
        finally:
            liberation.set()
            for processus_enfant in processus:
                processus_enfant.join(timeout=10)
                if processus_enfant.is_alive():
                    processus_enfant.terminate()
                    processus_enfant.join(timeout=5)
            resultats.close()
            resultats.join_thread()
            for processus_enfant in processus:
                processus_enfant.close()
        erreurs = [message for message in messages if message[0] == "error"]
        self.assertFalse(erreurs, erreurs)
        self.assertEqual(sum(message[0] == "entered" for message in messages), 1,
                         messages)


class TestsDefautsAsynchrones(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_async_")
        self.racine = Path(self.temporaire.name)
        self.home = self.racine / "home"
        self.cwd = self.racine / "cwd"
        self.home.mkdir()
        self.cwd.mkdir()
        self.ancien_cwd = Path.cwd()
        self.ancien_home = os.environ.get("BLINK_HOME")
        os.environ["BLINK_HOME"] = str(self.home)
        os.chdir(self.cwd)

    async def asyncTearDown(self):
        os.chdir(self.ancien_cwd)
        if self.ancien_home is None:
            os.environ.pop("BLINK_HOME", None)
        else:
            os.environ["BLINK_HOME"] = self.ancien_home
        self.temporaire.cleanup()

    def arguments(self, **changements):
        valeurs = dict(
            since=None, camera=None, command="list", output=self.home / "clips",
            hub=None, overwrite=False, source="cloud", loop=None,
        )
        valeurs.update(changements)
        return SimpleNamespace(**valeurs)

    @unittest.expectedFailure
    async def test_B01_cloud_vide_retourne_un_tuple(self):
        """B-01 : un inventaire vide est un succès `(False, 0)`."""
        with mock.patch.object(b2v, "read_cloud_manifest",
                               new=mock.AsyncMock(return_value=[])):
            resultat = await b2v.traiter_cloud(object(), self.arguments(), [])
        self.assertEqual(resultat, (False, 0))

    @unittest.expectedFailure
    async def test_B01_filtre_camera_vide_retourne_un_tuple(self):
        """B-01 : un filtre sans résultat conserve le même contrat."""
        with mock.patch.object(b2v, "read_cloud_manifest",
                               new=mock.AsyncMock(return_value=[FauxClip()] )):
            resultat = await b2v.traiter_cloud(
                object(), self.arguments(camera="absente"), [],
            )
        self.assertEqual(resultat, (False, 0))

    @unittest.expectedFailure
    async def test_B02_http_404_non_vide_n_est_pas_enregistre_comme_video(self):
        """B-02 : un corps HTML non vide avec statut 404 est un échec."""
        clip = b2v.CloudClip({
            "id": 1, "device_name": "jardin",
            "created_at": "2026-08-13T12:00:00+00:00",
            "media": "https://example.invalid/media", "network_id": 7,
        })
        cible = self.home / "404.mp4.part"
        resultat = await clip.download_to(
            FauxBlinkHTTP(FauxReponse(404, b"<html>not found</html>")), cible,
        )
        self.assertFalse(resultat)
        self.assertFalse(cible.exists())

    @unittest.expectedFailure
    async def test_I05_fichier_cloud_absent_est_repare(self):
        """I-05 : registre sans média n'est pas un doublon définitif."""
        sortie = self.home / "clips"
        sortie.mkdir()
        clip = FauxClip()

        async def telecharger(_blink, cible):
            cible.parent.mkdir(parents=True, exist_ok=True)
            cible.write_bytes(b"video")
            return True

        clip.download_to = telecharger
        (sortie / b2v.STATE_FILENAME).write_text(json.dumps({
            "version": 1,
            "clips": {"ancien": {
                "camera": clip.name,
                "created_at": clip.created_at.isoformat(),
                "path": "fichier-disparu.mp4",
                "source": "cloud",
            }},
        }), encoding="utf-8")
        with mock.patch.object(b2v, "read_cloud_manifest",
                               new=mock.AsyncMock(return_value=[clip])):
            with contextlib.redirect_stdout(io.StringIO()):
                resultat = await b2v.traiter_cloud(
                    object(), self.arguments(command="download", output=sortie), [],
                )
        self.assertEqual(resultat, (False, 1))

    @unittest.expectedFailure
    async def test_I09_compte_cloud_only_accepte_login(self):
        """I-09 : login ne dépend pas de la présence d'un Sync Module."""
        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        blink = SimpleNamespace(sync={})
        with mock.patch.object(b2v, "ClientSession", Session), \
             mock.patch.object(b2v, "connect", new=mock.AsyncMock(return_value=blink)), \
             contextlib.redirect_stdout(io.StringIO()):
            code = await b2v.main(self.arguments(command="login"))
        self.assertEqual(code, 0)

    @unittest.expectedFailure
    async def test_I16_noms_distincts_ne_collisionnent_pas_apres_assainissement(self):
        """I-16 : safe_name sert au chemin, jamais à l'identité métier."""
        local = FauxClip(1, nom="Entrée/jardin")
        cloud = FauxClip(2, nom="Entrée_jardin")
        inedits, doublons = b2v.rapprocher([local], [cloud])
        self.assertEqual([clip.id for clip in inedits], [2])
        self.assertEqual(doublons, [])

    @unittest.expectedFailure
    async def test_B06_usb_prend_le_verrou_du_hub(self):
        """B-06 : manifeste et téléchargement USB réservent le Sync Module."""
        noms = []

        @contextlib.contextmanager
        def verrou_trace(nom, *_args, **_kwargs):
            noms.append(nom)
            yield

        sync = FauxSync()
        with mock.patch.object(runtime, "verrou", side_effect=verrou_trace), \
             mock.patch.object(b2v, "read_local_manifest",
                               new=mock.AsyncMock(return_value=[FauxClip()])), \
             mock.patch.object(b2v, "download_clip",
                               new=mock.AsyncMock(return_value="failed")), \
             mock.patch.object(b2v, "save_download_state"), \
             mock.patch.object(runtime, "travail"), \
             contextlib.redirect_stdout(io.StringIO()):
            await b2v.un_passage(
                object(), self.arguments(command="download", source="usb"),
                [("Test", sync)],
            )
        self.assertIn("hub", noms)

    @unittest.expectedFailure
    async def test_I10_entree_de_registre_non_objet_n_abat_pas_le_cloud(self):
        """I-10 : une entrée corrompue est isolée du reste du registre."""
        sortie = self.home / "clips"
        sortie.mkdir()
        (sortie / b2v.STATE_FILENAME).write_text(json.dumps({
            "version": 1, "clips": {"corrompue": 17},
        }), encoding="utf-8")
        with mock.patch.object(b2v, "read_cloud_manifest",
                               new=mock.AsyncMock(return_value=[FauxClip()])), \
             contextlib.redirect_stdout(io.StringIO()):
            resultat = await b2v.traiter_cloud(
                object(), self.arguments(output=sortie), [],
            )
        self.assertEqual(resultat, (False, 0))

    @unittest.expectedFailure
    async def test_I07_depuis_zero_signifie_aujourd_hui(self):
        """I-07 : `--since 0` ne doit pas se transformer en trente jours."""
        appels = []

        class Blink:
            async def get_videos_metadata(self, **options):
                appels.append(options)
                return []

        avant = dt.datetime.now() - dt.timedelta(days=1)
        await b2v.read_cloud_manifest(Blink(), 0)
        depuis = dt.datetime.strptime(appels[0]["since"], "%Y/%m/%d %H:%M:%S")
        self.assertGreaterEqual(depuis, avant)


@unittest.skip("E-01 : serveur d'onboarding prévu à l'étape 5, API absente à l'étape 0")
class TestsOnboardingFutur(unittest.TestCase):
    def test_E01_login_web_simple(self):
        pass

    def test_E01_login_web_2fa(self):
        pass

    def test_E01_annulation_timeout_et_navigateur_absent(self):
        pass

    def test_E01_mini_smoke_precede_les_workers(self):
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
