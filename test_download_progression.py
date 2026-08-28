"""Non-régression de la progression globale des nouveaux clips."""

import contextlib
import datetime as dt
import io
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import blink_engine
import blink_cli
import blink_models
import runtime
import serve


VIDEO_VALIDE = b"    ftyp" + b"\x00" * 56


class FauxSync:
    def __init__(self, identifiant, reseau):
        self.sync_id = identifiant
        self.network_id = reseau


class FauxClip:
    def __init__(self, identifiant, camera, instant, reseau, appareil):
        self.id = identifiant
        self.name = camera
        self.created_at = instant
        self.network_id = reseau
        self.device_id = appareil
        self.size = 1
        self.download_issue = None
        self.telechargements = 0

    async def download_to(self, _blink, cible):
        self.telechargements += 1
        cible.parent.mkdir(parents=True, exist_ok=True)
        cible.write_bytes(VIDEO_VALIDE)
        return True

    async def delete_video(self, _blink):
        return True


class ProgressionGlobaleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_progress_")
        self.home = Path(self.temporaire.name)
        self.ancien_home = os.environ.get("BLINK_HOME")
        os.environ["BLINK_HOME"] = str(self.home)

    async def asyncTearDown(self):
        if self.ancien_home is None:
            os.environ.pop("BLINK_HOME", None)
        else:
            os.environ["BLINK_HOME"] = self.ancien_home
        self.temporaire.cleanup()

    async def test_multi_modules_cloud_et_repli_gardent_un_total_unique(self):
        """Deux modules + cloud donnent un seul 0/N…N/N sans doublon.

        Le premier clip USB échoue mais existe aussi dans le cloud : ce dernier
        est son repli et conserve le même cran, au lieu d'agrandir ou remettre
        à zéro la barre.
        """
        base = dt.datetime(2026, 8, 28, 10, tzinfo=dt.timezone.utc)
        sync_a = FauxSync(10, 7)
        sync_b = FauxSync(20, 8)
        usb_repli = FauxClip("usb-a", "Entrée", base, 7, "cam-a")
        usb_ok = FauxClip("usb-b", "Jardin", base + dt.timedelta(minutes=1), 8, "cam-b")
        cloud_repli = FauxClip(
            "cloud-a", "Entrée", base + dt.timedelta(seconds=1), 7, "cam-a",
        )
        cloud_c = FauxClip(
            "cloud-c", "Garage", base + dt.timedelta(minutes=2), 7, "cam-c",
        )
        cloud_d = FauxClip(
            "cloud-d", "Salon", base + dt.timedelta(minutes=3), 7, "cam-d",
        )
        locaux = {sync_a: [usb_repli], sync_b: [usb_ok]}
        appels_travail = []

        async def lire_local(sync):
            return locaux[sync]

        async def telecharger_usb(_blink, clip, cible, _overwrite):
            if clip is usb_repli:
                return "failed"
            cible.parent.mkdir(parents=True, exist_ok=True)
            cible.write_bytes(VIDEO_VALIDE)
            return "downloaded"

        def publier(quoi, fait=0, total=0, cle=None):
            appels_travail.append((quoi, fait, total, cle))

        arguments = SimpleNamespace(
            since=None,
            camera=None,
            command="download",
            output=self.home / "clips",
            hub=None,
            overwrite=False,
            source="all",
            loop=None,
        )
        with mock.patch.object(
            blink_models, "read_local_manifest", side_effect=lire_local,
        ), mock.patch.object(
            blink_models, "read_cloud_manifest",
            new=mock.AsyncMock(return_value=[cloud_repli, cloud_c, cloud_d]),
        ), mock.patch.object(
            blink_engine, "download_clip", side_effect=telecharger_usb,
        ), mock.patch.object(
            runtime, "travail", side_effect=publier,
        ), mock.patch.object(
            runtime, "lire_suppression_auto", return_value=set(),
        ), mock.patch.object(runtime, "marquer"), \
             mock.patch.object(runtime, "toast"), \
             mock.patch.object(runtime, "lire_langue", return_value="fr"), \
             mock.patch.object(runtime, "lire_reglages", return_value={"port": 8765}), \
             contextlib.redirect_stdout(io.StringIO()) as sortie:
            code = await blink_engine.un_passage(
                object(), arguments, [("Maison", sync_a), ("Annexe", sync_b)],
            )

        progression = [
            (fait, total)
            for _quoi, fait, total, cle in appels_travail
            if cle == "phase.download_clips"
        ]
        self.assertTrue(progression)
        self.assertEqual(progression[0], (0, 4))
        self.assertEqual(progression[-1], (4, 4))
        self.assertTrue(all(total == 4 for _fait, total in progression))
        self.assertEqual(
            [fait for fait, _total in progression],
            sorted(fait for fait, _total in progression),
        )
        self.assertIn("[1/4] 0%", sortie.getvalue())
        self.assertIn("[4/4] 100%", sortie.getvalue())
        self.assertEqual(cloud_repli.telechargements, 1)
        self.assertEqual(cloud_c.telechargements, 1)
        self.assertEqual(cloud_d.telechargements, 1)
        # Le repli a sauvé le média : l'échec de sa première source reste dans
        # le journal, mais ne transforme pas le passage global en échec.
        self.assertEqual(code, 0)

    async def test_un_echec_definitif_termine_quand_meme_la_barre(self):
        """Un clip traité mais refusé compte comme fini, sans rester à 0/N."""
        sync = FauxSync(10, 7)
        clip = FauxClip(
            "usb-echec", "Entrée",
            dt.datetime(2026, 8, 28, 10, tzinfo=dt.timezone.utc),
            7, "cam-a",
        )
        appels_travail = []
        arguments = SimpleNamespace(
            since=None,
            camera=None,
            command="download",
            output=self.home / "clips-echec",
            hub=None,
            overwrite=False,
            source="usb",
            loop=None,
        )

        def publier(quoi, fait=0, total=0, cle=None):
            appels_travail.append((fait, total, cle))

        with mock.patch.object(
            blink_models, "read_local_manifest",
            new=mock.AsyncMock(return_value=[clip]),
        ), mock.patch.object(
            blink_engine, "download_clip",
            new=mock.AsyncMock(return_value="failed"),
        ), mock.patch.object(runtime, "travail", side_effect=publier), \
             mock.patch.object(runtime, "lire_suppression_auto", return_value=set()), \
             mock.patch.object(runtime, "marquer"), \
             contextlib.redirect_stdout(io.StringIO()) as sortie:
            code = await blink_engine.un_passage(
                object(), arguments, [("Maison", sync)],
            )

        progression = [
            (fait, total) for fait, total, cle in appels_travail
            if cle == "phase.download_clips"
        ]
        self.assertEqual(progression[0], (0, 1))
        self.assertEqual(progression[-1], (1, 1))
        self.assertIn("[1/1] 100%", sortie.getvalue())
        self.assertEqual(code, 1)

    async def test_doublon_cloud_deja_reussi_en_usb_ne_relance_pas_un_spinner(self):
        instant = dt.datetime(2026, 8, 28, 11, tzinfo=dt.timezone.utc)
        sync = FauxSync(10, 7)
        usb = FauxClip("usb", "Entrée", instant, 7, "cam-a")
        cloud = FauxClip(
            "cloud", "Entrée", instant + dt.timedelta(seconds=1), 7, "cam-a",
        )
        arguments = SimpleNamespace(
            since=None,
            camera=None,
            command="download",
            output=self.home / "clips-doublon",
            hub=None,
            overwrite=False,
            source="all",
            loop=None,
        )

        async def telecharger_usb(_blink, _clip, cible, _overwrite):
            cible.parent.mkdir(parents=True, exist_ok=True)
            cible.write_bytes(VIDEO_VALIDE)
            return "downloaded"

        with mock.patch.object(
            blink_models, "read_local_manifest",
            new=mock.AsyncMock(return_value=[usb]),
        ), mock.patch.object(
            blink_models, "read_cloud_manifest",
            new=mock.AsyncMock(return_value=[cloud]),
        ), mock.patch.object(
            blink_engine, "download_clip", side_effect=telecharger_usb,
        ), mock.patch.object(runtime, "travail"), \
             mock.patch.object(runtime, "lire_suppression_auto", return_value=set()), \
             mock.patch.object(runtime, "marquer"), \
             mock.patch.object(runtime, "toast"), \
             mock.patch.object(runtime, "lire_langue", return_value="fr"), \
             mock.patch.object(runtime, "lire_reglages", return_value={"port": 8765}), \
             contextlib.redirect_stdout(io.StringIO()) as sortie:
            code = await blink_engine.un_passage(
                object(), arguments, [("Maison", sync)],
            )

        self.assertEqual(code, 0)
        self.assertEqual(cloud.telechargements, 0)
        self.assertNotIn("=== CLOUD DE L'ABONNEMENT ===", sortie.getvalue())
        self.assertIn("[1/1] 100%", sortie.getvalue())

    async def test_exception_de_suppression_usb_n_interrompt_pas_le_lot(self):
        sync = FauxSync(10, 7)
        instant = dt.datetime(2026, 8, 28, 12, tzinfo=dt.timezone.utc)
        premier = FauxClip("usb-1", "Entrée", instant, 7, "cam-a")
        second = FauxClip(
            "usb-2", "Jardin", instant + dt.timedelta(minutes=1), 7, "cam-b",
        )
        premier.delete_video = mock.AsyncMock(side_effect=RuntimeError("API indisponible"))
        appels_travail = []
        arguments = SimpleNamespace(
            since=None, camera=None, command="download",
            output=self.home / "clips-suppression", hub=None,
            overwrite=False, source="usb", loop=None,
        )

        async def telecharger(_blink, _clip, cible, _overwrite):
            cible.parent.mkdir(parents=True, exist_ok=True)
            cible.write_bytes(VIDEO_VALIDE)
            return "downloaded"

        with mock.patch.object(
            blink_models, "read_local_manifest",
            new=mock.AsyncMock(return_value=[premier, second]),
        ), mock.patch.object(
            blink_engine, "download_clip", side_effect=telecharger,
        ), mock.patch.object(
            runtime, "travail",
            side_effect=lambda _q, fait=0, total=0, cle=None:
                appels_travail.append((fait, total, cle)),
        ), mock.patch.object(
            runtime, "lire_suppression_auto", return_value={"Entrée", "Jardin"},
        ), mock.patch.object(runtime, "marquer"), \
             mock.patch.object(runtime, "toast"), \
             mock.patch.object(runtime, "lire_langue", return_value="fr"), \
             mock.patch.object(runtime, "lire_reglages", return_value={"port": 8765}), \
             contextlib.redirect_stdout(io.StringIO()) as sortie:
            code = await blink_engine.un_passage(
                object(), arguments, [("Maison", sync)],
            )

        progression = [
            (fait, total) for fait, total, cle in appels_travail
            if cle == "phase.download_clips"
        ]
        self.assertEqual(code, 0)
        self.assertEqual(progression[-1], (2, 2))
        self.assertIn("[2/2] 100%", sortie.getvalue())
        self.assertIn("Suppression impossible", sortie.getvalue())

    async def test_planificateur_commence_global_puis_respecte_les_deux_cadences(self):
        horloge = {"t": 0.0}
        sources = []

        async def faire(_blink, arguments, _modules, repetition):
            self.assertTrue(repetition)
            sources.append(arguments.source)
            return blink_engine._ResultatPassage(0, True)

        async def attendre(echeance):
            horloge["t"] = echeance
            return True

        arguments = SimpleNamespace(source="usb", usb_loop=2, cloud_loop=1, loop=None)
        with mock.patch.object(blink_engine, "_faire_passage", side_effect=faire), \
             mock.patch.object(blink_engine, "_attendre_echeance", side_effect=attendre), \
             mock.patch.object(blink_engine.time, "monotonic",
                               side_effect=lambda: horloge["t"]), \
             mock.patch.object(runtime, "arret_demande",
                               side_effect=lambda: len(sources) >= 3):
            code = await blink_engine._boucler_sources(
                object(), arguments, [("Maison", FauxSync(10, 7))], 2, 1,
            )

        self.assertEqual(code, 0)
        self.assertEqual(sources, ["all", "cloud", "all"])
        self.assertEqual(arguments.source, "usb", "les copies ne mutent pas la CLI")

    async def test_bootstrap_global_est_reessaye_apres_contention(self):
        sources = []
        arrete = {"oui": False}

        async def faire(_blink, arguments, _modules, repetition):
            sources.append(arguments.source)
            if len(sources) == 1:
                return blink_engine._ResultatPassage(0, False)
            arrete["oui"] = True
            return blink_engine._ResultatPassage(0, True)

        with mock.patch.object(blink_engine, "_faire_passage", side_effect=faire), \
             mock.patch.object(
                 blink_engine, "_attendre_echeance", new=mock.AsyncMock(return_value=True),
             ), mock.patch.object(runtime, "arret_demande",
                                  side_effect=lambda: arrete["oui"]):
            await blink_engine._boucler_sources(
                object(), SimpleNamespace(source="all"), [], 10, 1,
            )

        self.assertEqual(sources, ["all", "all"])

    async def test_contention_d_un_clic_manuel_n_est_pas_un_faux_succes(self):
        class VerrouOccupe:
            def __enter__(self):
                raise runtime.BusyError("worker automatique")

            def __exit__(self, *_args):
                return False

        with mock.patch.object(runtime, "verrou", return_value=VerrouOccupe()), \
             contextlib.redirect_stdout(io.StringIO()):
            resultat = await blink_engine._faire_passage(
                object(), SimpleNamespace(), [], repetition=False,
            )

        self.assertEqual(resultat, blink_engine._ResultatPassage(1, False))


class ProgressionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_work_state_")
        self.ancien_home = os.environ.get("BLINK_HOME")
        os.environ["BLINK_HOME"] = self.temporaire.name

    def tearDown(self):
        if self.ancien_home is None:
            os.environ.pop("BLINK_HOME", None)
        else:
            os.environ["BLINK_HOME"] = self.ancien_home
        self.temporaire.cleanup()

    def test_fin_n_n_reste_affichable_sans_rester_active(self):
        runtime.travail(
            "Téléchargement des clips", 1, 1, cle="phase.download_clips",
        )
        runtime.fin_travail(conserver=10)

        self.assertEqual(runtime.travail_en_cours(), {})
        visible = runtime.travail_affichable()
        self.assertEqual((visible["fait"], visible["total"]), (1, 1))
        self.assertTrue(visible["termine"])
        self.assertFalse(visible["actif"])

    def test_download_termine_et_merge_actif_ne_s_ecrasent_pas(self):
        with mock.patch.object(runtime, "processus_vivant", return_value=True):
            with mock.patch.object(runtime.os, "getpid", return_value=101):
                runtime.travail(
                    "Téléchargement des clips", 4, 4,
                    cle="phase.download_clips",
                )
                runtime.fin_travail(conserver=10)
            with mock.patch.object(runtime.os, "getpid", return_value=202):
                runtime.travail(
                    "Assemblage des vidéos", 1, 3,
                    cle="phase.merge_videos",
                )

            visible = runtime.travail_affichable()
            self.assertEqual(visible["pid"], 101)
            self.assertTrue(visible["termine"])
            self.assertTrue(visible["actif"], "le merge actif doit garder le bouton bloqué")
            self.assertEqual(runtime.travail_en_cours()["pid"], 202)

            with mock.patch.object(runtime.os, "getpid", return_value=202):
                runtime.fin_travail()
            self.assertEqual(runtime.travail_en_cours(), {})
            self.assertEqual(runtime.travail_affichable()["pid"], 101)

    def test_echec_du_snapshot_terminal_ne_laisse_pas_un_faux_actif(self):
        runtime.travail(
            "Téléchargement des clips", 1, 1, cle="phase.download_clips",
        )
        with mock.patch.object(runtime, "_ecrire_fiche_travail", return_value=False):
            runtime.fin_travail(conserver=10)
        self.assertEqual(runtime.travail_en_cours(), {})


class ProgressionCliTests(unittest.TestCase):
    def test_cadences_internes_acceptent_un_download_global(self):
        argv = [
            "blink2video", "download", "--from", "all",
            "--usb-loop", "10", "--cloud-loop", "1",
        ]
        with mock.patch.object(sys, "argv", argv):
            arguments = blink_cli.parse_args()
        self.assertEqual((arguments.usb_loop, arguments.cloud_loop), (10, 1))

    def test_une_seule_cadence_interne_est_refusee(self):
        argv = ["blink2video", "download", "--from", "all", "--usb-loop", "10"]
        with mock.patch.object(sys, "argv", argv), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            blink_cli.parse_args()


class ProgressionInterfaceTests(unittest.TestCase):
    def test_api_travail_ne_relit_pas_le_registre_de_clips(self):
        reponses = []
        faux = SimpleNamespace(
            path="/api/travail",
            hote_autorise=lambda: True,
            send_json=lambda corps, *_args: reponses.append(corps),
        )
        travail = {"quoi": "Téléchargement", "fait": 2, "total": 8}
        with mock.patch.object(runtime, "travail_affichable", return_value=travail), \
             mock.patch.object(
                 serve, "read_entries",
                 side_effect=AssertionError("registre relu par la sonde rapide"),
             ):
            serve.Handler.do_GET(faux)

        self.assertEqual(reponses, [{"travail": travail}])

    def test_suivre_transmet_explicitement_zero_et_cent_pour_cent(self):
        class Processus:
            def __init__(self):
                self.stdout = io.StringIO(
                    "  [1/2] 0%\n"
                    "  [1/2] clip-a.mp4\n"
                    "  [1/2] 100%\n"
                    "  [2/2] clip-b.mp4\n"
                    "  [2/2] 100%\n"
                )
                self.returncode = 0

            def wait(self):
                return self.returncode

            def terminate(self):
                self.returncode = 1

        evenements = []
        faux = SimpleNamespace(
            send_event=lambda evenement: evenements.append(evenement) or True,
        )
        with mock.patch.object(runtime, "demarrer", return_value=Processus()):
            resultat = serve.Handler.suivre(faux, ["download"], {}, "Téléchargement")

        progressions = [e["progress"] for e in evenements if "progress" in e]
        self.assertTrue(resultat)
        self.assertEqual(progressions[0], {"done": 0.0, "total": 2})
        self.assertEqual(progressions[-1], {"done": 2.0, "total": 2})

    def test_actualiser_ne_preverrouille_pas_le_hub_du_downloader(self):
        """Le faux enfant doit pouvoir devenir l'unique propriétaire du hub."""
        with tempfile.TemporaryDirectory(prefix="blink_refresh_lock_") as dossier:
            ancien_home = os.environ.get("BLINK_HOME")
            os.environ["BLINK_HOME"] = dossier
            execute = []
            evenements = []

            def run_refresh():
                with blink_engine.hub_lock("downloader-enfant"):
                    execute.append(True)

            faux = SimpleNamespace(
                lock=threading.Lock(),
                send_response=lambda *_args: None,
                send_header=lambda *_args: None,
                end_headers=lambda: None,
                send_event=lambda evenement: evenements.append(evenement) or True,
                run_refresh=run_refresh,
            )
            try:
                with mock.patch.object(runtime, "travail_en_cours", return_value={}), \
                     mock.patch.object(serve, "MODULE_SLOT", threading.Semaphore(1)):
                    serve.Handler.stream_refresh(faux)
            finally:
                if ancien_home is None:
                    os.environ.pop("BLINK_HOME", None)
                else:
                    os.environ["BLINK_HOME"] = ancien_home

        self.assertEqual(execute, [True])
        self.assertFalse(any(e.get("ok") is False for e in evenements))

    def test_polling_reste_rapide_pour_un_worker_demarre_plus_tard(self):
        self.assertIn('fetch("/api/travail", { cache: "no-store" })', serve.PAGE)
        self.assertIn("etatDuTravail();\n(function veillerTravail()", serve.PAGE)
        self.assertIn("function veillerTravail()", serve.PAGE)
        self.assertIn("veillerTravail();\n  }, 3000);", serve.PAGE)
        self.assertIn("veillerPassages();\n  }, 60000);", serve.PAGE)
        self.assertIn("let travailVisible = false;", serve.PAGE)


if __name__ == "__main__":
    unittest.main()
