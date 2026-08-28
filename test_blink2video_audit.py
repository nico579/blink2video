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
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parent
os.environ["BLINK_BOOTSTRAP"] = "none"

import blink2video as b2v  # noqa: E402 - bootstrap neutralisé avant import
import blink_auth  # noqa: E402
import blink_cli  # noqa: E402
import blink_engine  # noqa: E402
import blink_models  # noqa: E402
import blink_registre  # noqa: E402
import maj  # noqa: E402
import runtime  # noqa: E402 - bootstrap neutralisé avant import
import tray  # noqa: E402
import watch  # noqa: E402


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


class FauxSessionHTTP:
    """Contexte aiohttp minimal : aucune socket n'est ouverte par les tests."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class DateHeureFigee(dt.datetime):
    """Horloge déterministe pour vérifier précisément la fenêtre ``--since``."""

    INSTANT = dt.datetime(2026, 8, 13, 12, 34, 56)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.INSTANT
        return cls.INSTANT.replace(tzinfo=tz)


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
        with mock.patch.object(blink_auth, "CONFIG", session):
            chargee = blink_auth.load_saved_session()
        self.assertEqual(chargee["password"], "")
        self.assertEqual(chargee["refresh_token"], "jeton-factice")

    def test_G03_etat_absent_est_un_registre_vide(self):
        self.assertEqual(
            blink_registre.load_download_state(self.home / "clips"),
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
        inedits, doublons = blink_models.rapprocher([local], [proche, loin], tolerance=2)
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
            asyncio.run(blink_engine.download_clip(object(), ClipEnErreur(), cible, False))
        self.assertFalse(cible.with_suffix(".mp4.part").exists())

    def test_I15_usb_fichier_non_mp4_n_est_pas_acquis(self):
        """I-15 : un .part non vide mais sans boîte ftyp n'est pas un succès
        (un octet suffisait auparavant à passer pour un téléchargement)."""
        cible = self.home / "clips" / "invalide.mp4"

        class ClipCorrompu(FauxClip):
            async def prepare_download(self, _blink):
                return True

            async def download_video(self, _blink, chemin):
                Path(chemin).parent.mkdir(parents=True, exist_ok=True)
                Path(chemin).write_bytes(b"\x00")  # un octet, pas un MP4
                return True

        resultat = asyncio.run(
            blink_engine.download_clip(object(), ClipCorrompu(), cible, False))
        self.assertEqual(resultat, "failed")
        self.assertFalse(cible.exists())
        self.assertFalse(cible.with_suffix(".mp4.part").exists())

    def test_I15_usb_fichier_mp4_valide_est_acquis(self):
        """Non-régression : une boîte ftyp réelle reste acceptée."""
        cible = self.home / "clips" / "valide.mp4"

        class ClipValide(FauxClip):
            async def prepare_download(self, _blink):
                return True

            async def download_video(self, _blink, chemin):
                Path(chemin).parent.mkdir(parents=True, exist_ok=True)
                Path(chemin).write_bytes(
                    b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100)
                return True

        resultat = asyncio.run(
            blink_engine.download_clip(object(), ClipValide(), cible, False))
        self.assertEqual(resultat, "downloaded")
        self.assertTrue(cible.is_file())

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
    def test_I01_BLINK_HOME_est_la_racine_de_config_et_sortie(self):
        """I-01 : CONFIG et OUTPUT ne doivent jamais dépendre du CWD."""
        code = (
            "import json, blink_auth, blink_registre; "
            "print(json.dumps([str(blink_auth.CONFIG.resolve()), "
            "str(blink_registre.OUTPUT.resolve())]))"
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

    def _sans_blinkpy_ni_aiohttp(self, code: str) -> subprocess.CompletedProcess:
        """Exécute `code` dans un sous-processus où aiohttp et blinkpy ne
        peuvent jamais être importés (O-06/8.7/8.8) : un import accidentel se
        traduit par un ImportError immédiat et net plutôt que par un succès
        muet qui ne prouverait rien."""
        # sys.modules[nom] = None est le mécanisme documenté de l'import
        # système : le prochain « import nom » lève ImportError tout de
        # suite, sans passer par un faux Loader dont l'API (find_module/
        # load_module) est justement celle que Python 3.12 a cessé d'appeler.
        garde = (
            "import sys\n"
            "for _nom in ('aiohttp', 'blinkpy', 'blinkpy.auth', 'blinkpy.blinkpy'):\n"
            "    sys.modules[_nom] = None\n"
        )
        env = dict(os.environ, BLINK_HOME=str(self.home), BLINK_BOOTSTRAP="none",
                   PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, "-B", "-c", garde + code], cwd=self.cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", check=False,
            timeout=15,
        )

    def test_O06_stop_fonctionne_sans_aiohttp_ni_blinkpy(self):
        """O-06/8.7/8.8 : « stop » n'a besoin ni de Blink ni du réseau."""
        resultat = self._sans_blinkpy_ni_aiohttp(
            "import blink2video; print(blink2video.route(['stop']))"
        )
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        self.assertIn("Rien ne tourne.", resultat.stdout)

    def test_O06_aide_fonctionne_sans_aiohttp_ni_blinkpy(self):
        """O-06/8.7/8.8 : --help ne doit jamais tirer Blink derrière lui."""
        resultat = self._sans_blinkpy_ni_aiohttp(
            "import blink2video; blink2video.route(['--help'])"
        )
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        self.assertIn("Verbes :", resultat.stdout)

    def test_O06_open_fonctionne_sans_aiohttp_ni_blinkpy(self):
        """O-06/8.7/8.8 : « open » ne parle qu'à une socket locale."""
        resultat = self._sans_blinkpy_ni_aiohttp(
            "import blink2video; print(blink2video.route(['open', '--port', '1']))"
        )
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        self.assertIn("Personne n'écoute", resultat.stdout)

    def test_O06_update_delegue_fonctionne_sans_aiohttp_ni_blinkpy(self):
        """O-06/8.7/8.8 : « update --help » ne doit pas non plus les tirer."""
        resultat = self._sans_blinkpy_ni_aiohttp(
            "import blink2video; blink2video.route(['update', '--help'])"
        )
        self.assertEqual(resultat.returncode, 0, resultat.stderr)

    def test_O06_download_a_bien_besoin_de_blinkpy(self):
        """Contre-épreuve : un verbe qui parle à Blink échoue proprement
        quand blinkpy est absent, au lieu de planter plus tard sans message
        clair. Prouve aussi que la garde du test bloque réellement l'import :
        runtime.bootstrap(), appelé au bon moment par main(), détecte
        l'absence via importlib.util.find_spec et l'annonce proprement,
        plutôt que de laisser un ImportError remonter sans contexte."""
        resultat = self._sans_blinkpy_ni_aiohttp(
            "import blink2video; blink2video.route(['login'])"
        )
        self.assertNotEqual(resultat.returncode, 0)
        self.assertIn("Dépendances absentes", resultat.stdout)
        self.assertIn("aiohttp", resultat.stdout)
        self.assertIn("blinkpy", resultat.stdout)

    def test_I02_mot_de_passe_absent_meme_present_dans_login_attributes(self):
        """I-02/4.5 : liste blanche, le mot de passe ne sort jamais."""
        session = self.home / "blink_auth.json"

        class AuthFactice:
            login_attributes = {
                "username": "personne@example.invalid",
                "password": "ne-doit-jamais-etre-ecrit",
                "token": "abc", "refresh_token": "def",
                "champ_futur_inconnu": "ne-doit-pas-sortir-non-plus",
            }

        blink = SimpleNamespace(auth=AuthFactice())
        with mock.patch.object(blink_auth, "CONFIG", session):
            blink_auth.save_session(blink)
            ecrit = json.loads(session.read_text(encoding="utf-8"))
        self.assertNotIn("password", ecrit)
        self.assertNotIn("champ_futur_inconnu", ecrit)
        self.assertEqual(ecrit["token"], "abc")

    def test_I02_temporaire_propre_a_ce_processus_puis_efface(self):
        """I-02 : plus de blink_auth.tmp partagé entre processus concurrents."""
        session = self.home / "blink_auth.json"
        blink = SimpleNamespace(auth=SimpleNamespace(
            login_attributes={"username": "x", "token": "y"}))
        with mock.patch.object(blink_auth, "CONFIG", session):
            blink_auth.save_session(blink)
        restants = list(self.home.glob("*.tmp"))
        self.assertEqual(restants, [], "aucun temporaire ne doit survivre à l'écriture")
        ancien_partage = self.home / "blink_auth.tmp"
        self.assertFalse(ancien_partage.exists())

    def test_I02_sauvegarde_en_retard_n_ecrase_pas_la_plus_recente(self):
        """I-02 : un rafraîchissement arrivé en désordre ne régresse pas."""
        session = self.home / "blink_auth.json"
        session.write_text(json.dumps({
            "token": "le-plus-recent", "updated_at": time.time() + 3600,
        }), encoding="utf-8")
        blink = SimpleNamespace(auth=SimpleNamespace(
            login_attributes={"token": "en-retard"}))
        with mock.patch.object(blink_auth, "CONFIG", session), \
             mock.patch.object(b2v.time, "time", return_value=1_000.0):
            blink_auth.save_session(blink)
        self.assertEqual(
            json.loads(session.read_text(encoding="utf-8"))["token"],
            "le-plus-recent",
        )

    def test_bug7_updated_at_malforme_ne_casse_pas_la_sauvegarde(self):
        """Revue du 27/08, bug 7 : un fichier de session par ailleurs valide
        contenant "updated_at": "invalide" faisait lever ValueError à
        float(), y compris depuis le callback automatique de
        rafraîchissement Blink. Traité comme aucune préférence connue (0) :
        la nouvelle sauvegarde doit l'emporter, pas planter."""
        session = self.home / "blink_auth.json"
        session.write_text(json.dumps({
            "token": "ancien", "updated_at": "invalide",
        }), encoding="utf-8")
        blink = SimpleNamespace(auth=SimpleNamespace(
            login_attributes={"token": "nouveau"}))
        with mock.patch.object(blink_auth, "CONFIG", session):
            blink_auth.save_session(blink)
        self.assertEqual(
            json.loads(session.read_text(encoding="utf-8"))["token"], "nouveau")

    def test_sauvegarde_session_attend_le_verrou_avant_d_ecrire(self):
        """Bug #7, revue de code du 0eab463 : lecture-decision-ecriture
        passe desormais par runtime.verrou() - une sauvegarde concurrente ne
        doit ni s'entrelacer (l'ancien trou) ni echouer aussitot, juste
        patienter le temps que le verrou se libere."""
        session = self.home / "blink_auth.json"
        blink = SimpleNamespace(auth=SimpleNamespace(
            login_attributes={"token": "nouveau"}))

        with mock.patch.object(blink_auth, "CONFIG", session):
            with runtime.verrou("session-save", "concurrent-test"):
                fil = threading.Thread(target=blink_auth.save_session, args=(blink,))
                fil.start()
                time.sleep(0.2)
                self.assertFalse(session.exists(),
                                 "n'aurait pas dû écrire pendant que le verrou est tenu")
            fil.join(timeout=10)
        self.assertFalse(fil.is_alive(), "save_session() ne rend jamais la main")
        self.assertEqual(
            json.loads(session.read_text(encoding="utf-8"))["token"], "nouveau")

    def test_I03_make_blink_fournit_un_callback_de_persistance(self):
        """I-03 : le rafraîchissement automatique de blinkpy doit être persisté."""
        session = self.home / "blink_auth.json"
        with mock.patch.object(blink_auth, "CONFIG", session):
            blink = blink_auth.make_blink(
                FauxSessionHTTP(), {"username": "x", "password": "y"},
            )
            self.assertIsNotNone(blink.auth.callback)
            blink.auth.refresh_token = "frais"
            blink.auth.callback()
            ecrit = json.loads(session.read_text(encoding="utf-8"))
        self.assertEqual(ecrit.get("refresh_token"), "frais")
        self.assertNotIn("password", ecrit)

    def test_win7_tls_ajoute_certifi_sans_remplacer_le_contexte_sur(self):
        """Win7 : les racines récentes complètent les contrôles TLS standards."""
        contexte = mock.Mock()
        with mock.patch.object(
            blink_auth.ssl, "create_default_context", return_value=contexte
        ) as creer, mock.patch.object(
            blink_auth.certifi, "where", return_value="racines-certifi.pem"
        ):
            self.assertIs(blink_auth.contexte_tls(), contexte)
        creer.assert_called_once_with()
        contexte.load_verify_locations.assert_called_once_with(
            cafile="racines-certifi.pem"
        )

    def test_win7_session_tls_attend_la_fermeture_du_transport(self):
        """Python 3.8 : la boucle ne doit pas mourir avant le transport SSL."""
        session = mock.Mock()
        session.close = mock.AsyncMock()

        async def utiliser():
            with mock.patch.object(
                blink_auth, "session_http", return_value=session
            ), mock.patch.object(
                blink_auth.asyncio, "sleep", new=mock.AsyncMock()
            ) as attendre:
                async with blink_auth.session_http_temporaire() as recue:
                    self.assertIs(recue, session)
                attendre.assert_awaited_once_with(0.250)

        asyncio.run(utiliser())
        session.close.assert_awaited_once_with()

    def test_win7_watch_reutilise_la_session_tls_et_la_ferme(self):
        """La surveillance doit conserver les racines certifi après le login."""
        session = SimpleNamespace(close=mock.AsyncMock())
        blink = SimpleNamespace(
            refresh=mock.AsyncMock(side_effect=RuntimeError("panne simulée"))
        )
        connecter = mock.AsyncMock(return_value=blink)
        attendre = mock.AsyncMock()

        with mock.patch.object(
            blink_auth, "session_http", return_value=session
        ) as fabriquer, mock.patch.object(
            blink_auth,
            "session_http_temporaire",
            wraps=blink_auth.session_http_temporaire,
        ) as temporaire, mock.patch.object(
            blink_auth, "connect_saved", new=connecter
        ), mock.patch.object(
            blink_auth.asyncio, "sleep", new=attendre
        ), mock.patch.object(
            watch,
            "ClientSession",
            side_effect=AssertionError("session directe interdite"),
            create=True,
        ) as directe:
            with self.assertRaisesRegex(RuntimeError, "panne simulée"):
                asyncio.run(watch.read_state(object()))

        fabriquer.assert_called_once_with()
        temporaire.assert_called_once_with()
        connecter.assert_awaited_once_with(session)
        blink.refresh.assert_awaited_once_with(force=True)
        session.close.assert_awaited_once_with()
        attendre.assert_awaited_once_with(0.250)
        directe.assert_not_called()

    def test_I10_session_json_tableau_est_refusee_sans_exception(self):
        """I-10 : une racine JSON valide mais non objet doit être tolérée."""
        session = self.home / "blink_auth.json"
        session.write_text("[]", encoding="utf-8")
        with mock.patch.object(blink_auth, "CONFIG", session):
            self.assertIsNone(blink_auth.load_saved_session())

    def test_I10_session_json_null_est_refusee_sans_exception(self):
        """I-10 : la racine JSON ``null`` ne doit pas atteindre ``.get``."""
        session = self.home / "blink_auth.json"
        session.write_text("null", encoding="utf-8")
        with mock.patch.object(blink_auth, "CONFIG", session):
            self.assertIsNone(blink_auth.load_saved_session())

    def test_I10_registre_json_tableau_est_refuse_sans_exception(self):
        """I-10 : le chargeur d'état valide le type avant .get()."""
        sortie = self.home / "clips"
        sortie.mkdir()
        (sortie / blink_registre.STATE_FILENAME).write_text("[]", encoding="utf-8")
        self.assertEqual(blink_registre.load_download_state(sortie),
                         {"version": 1, "clips": {}})

    def test_I10_registre_json_null_est_refuse_sans_exception(self):
        """I-10 : un registre ``null`` suit la stratégie du registre vide."""
        sortie = self.home / "clips"
        sortie.mkdir()
        (sortie / blink_registre.STATE_FILENAME).write_text("null", encoding="utf-8")
        self.assertEqual(blink_registre.load_download_state(sortie),
                         {"version": 1, "clips": {}})

    def test_I11_valeur_de_camera_egale_a_un_verbe_reste_une_valeur(self):
        """I-11 : `download --camera watch` forme une seule commande."""
        self.assertEqual(
            runtime.decouper_verbes(["download", "--camera", "watch"]),
            [["download", "--camera", "watch"]],
        )

    def test_I11_camera_update_ne_declenche_pas_une_mise_a_jour(self):
        """I-11 : `download --camera update` ne doit pas ouvrir « update »,
        qui arrêterait tout le reste de la composition en cours de route."""
        self.assertEqual(
            runtime.decouper_verbes(["download", "--camera", "update"]),
            [["download", "--camera", "update"]],
        )

    def test_I11_ignore_a_valeurs_multiples_avale_un_nom_de_verbe(self):
        """I-11 : `watch --ignore serve jardin` cible deux caméras, dont une
        nommée comme un verbe ; ni « serve » ni « jardin » n'ouvrent un
        nouveau groupe."""
        self.assertEqual(
            runtime.decouper_verbes(["watch", "--ignore", "serve", "jardin"]),
            [["watch", "--ignore", "serve", "jardin"]],
        )

    def test_I11_ignore_s_arrete_au_prochain_verbe_reel(self):
        """La consommation multiple de --ignore doit s'arrêter dès qu'un
        second groupe est réellement voulu, pas avaler toute la ligne."""
        self.assertEqual(
            runtime.decouper_verbes(
                ["watch", "--ignore", "jardin", "--loop", "10", "merge"]),
            [["watch", "--ignore", "jardin", "--loop", "10"], ["merge"]],
        )

    def test_I11_loop_seul_n_avale_pas_le_verbe_suivant(self):
        """--loop est nargs='?' : sans nombre après lui, le mot suivant reste
        un verbe à part entière."""
        self.assertEqual(
            runtime.decouper_verbes(["watch", "--loop", "download"]),
            [["watch", "--loop"], ["download"]],
        )

    def test_I11_loop_suivi_d_un_nombre_reste_sa_valeur(self):
        """Non-régression : --loop 10 continue de fonctionner normalement."""
        self.assertEqual(
            runtime.decouper_verbes(["watch", "--loop", "10", "merge", "--loop", "5"]),
            [["watch", "--loop", "10"], ["merge", "--loop", "5"]],
        )

    def test_I12_boucle_nulle_est_rejetee(self):
        """I-12 : zéro ne doit pas être accepté comme cadence."""
        parseur = argparse.ArgumentParser()
        runtime.ajouter_boucle(parseur)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parseur.parse_args(["--loop", "0"])

    def test_I12_boucle_negative_est_rejetee(self):
        """I-12 : une cadence négative créerait une boucle serrée."""
        parseur = argparse.ArgumentParser()
        runtime.ajouter_boucle(parseur)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parseur.parse_args(["--loop", "-1"])

    def test_I22_open_refuse_le_port_zero_avant_la_socket(self):
        """I-22 : zéro n'est pas un port utilisateur valide pour ``open``."""
        with mock.patch("socket.socket") as creer_socket, \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                blink_cli.ouvrir(["--port", "0"])
        creer_socket.assert_not_called()

    def test_I22_open_refuse_le_port_65536_avant_la_socket(self):
        """I-22 : la borne supérieure valide est 65535."""
        with mock.patch("socket.socket") as creer_socket, \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                blink_cli.ouvrir(["--port", "65536"])
        creer_socket.assert_not_called()

    def test_bug5_port_demande_extrait_loption_explicite(self):
        """Revue du 27/08, bug 5."""
        self.assertEqual(blink_cli._port_demande(["--port", "55432"]), 55432)
        self.assertIsNone(blink_cli._port_demande(["--timezone", "Europe/Paris"]))
        self.assertEqual(
            blink_cli._port_demande(["--timezone", "Europe/Paris", "--port", "55432"]),
            55432)

    def test_bug5_start_avec_port_explicite_regarde_ce_port(self):
        """Revue du 27/08, bug 5 : start --port 55432 vérifiait le port
        ENREGISTRÉ (8765 par défaut), ignorant l'option explicite tapée sur
        cette même commande, dès lors que ce port enregistré répondait."""
        with mock.patch.object(blink_cli, "_port_ouvert",
                               side_effect=lambda p: p == 55432), \
             mock.patch.object(blink_cli, "ouvrir", return_value=0) as ouverture:
            code = blink_cli.executer([["start", "--port", "55432"]])
        self.assertEqual(code, 0)
        ouverture.assert_called_once_with(["--port", "55432"])

    def test_bug5_start_sans_port_explicite_regarde_le_port_enregistre(self):
        """Contre-épreuve du précédent : sans --port explicite, le
        comportement historique (port enregistré) doit rester intact."""
        port_enregistre = runtime.lire_reglages()["port"]
        with mock.patch.object(blink_cli, "_port_ouvert",
                               side_effect=lambda p: p == port_enregistre), \
             mock.patch.object(blink_cli, "ouvrir", return_value=0) as ouverture:
            code = blink_cli.executer([["start"]])
        self.assertEqual(code, 0)
        ouverture.assert_called_once_with(["--port", str(port_enregistre)])

    def test_bug6_ctrlc_tue_la_descendance_et_attend(self):
        """Revue du 27/08, bug 6 : le finally n'appelait qu'un
        Popen.terminate() nu sur un Ctrl+C - ni descendance tuée (ffmpeg
        pouvait survivre), ni attente de la fin réelle. Doit maintenant
        passer par arreter_processus(avec_descendance=True) en repli, une
        fois le délai de grâce coopératif écoulé (revue du 27/08, arrêt
        coopératif : ici les process ne sortent jamais seuls, le repli doit
        donc jouer). Deux verbes persistants, pour vérifier que chacun des
        « lances » reçoit l'appel, pas juste le premier."""
        class FauxProcessus:
            def __init__(self, pid):
                self.pid = pid

            def poll(self):
                return None  # ne sort jamais seul : le repli doit jouer

        horloge = {"t": 0.0}

        def faux_monotonic():
            # Saute directement au-delà du délai de grâce (15 s) dès le
            # premier appel, pour ne pas attendre réellement dans le test.
            horloge["t"] += 20
            return horloge["t"]

        appels_sleep = {"n": 0}

        def faux_sleep(duree):
            appels_sleep["n"] += 1
            if appels_sleep["n"] == 1:
                raise KeyboardInterrupt

        with mock.patch.object(runtime, "demarrer",
                               return_value=FauxProcessus(4242)), \
             mock.patch.object(tray, "disponible", return_value=False), \
             mock.patch("time.sleep", side_effect=faux_sleep), \
             mock.patch("time.monotonic", side_effect=faux_monotonic), \
             mock.patch.object(runtime, "arreter_processus") as tuer, \
             contextlib.redirect_stdout(io.StringIO()):
            blink_cli.executer([["serve", "--loop"], ["watch", "--loop"]])
        self.assertEqual(tuer.call_count, 2)
        tuer.assert_called_with(4242, avec_descendance=True)

    def test_I13_parent_mort_ne_fait_pas_oublier_un_enfant_vivant(self):
        """I-13 : une fiche reste utile tant qu'un de ses membres vit."""
        dossier = self.home / runtime.INSTANCES
        dossier.mkdir()
        fiche = dossier / "111.json"
        fiche.write_text(json.dumps({
            "pid": 111, "depuis": "2026-08-13T12:00:00+02:00",
            "verbes": [["serve"]], "enfants": [222],
        }), encoding="utf-8")
        with mock.patch.object(runtime, "processus_correspond",
                               side_effect=lambda pid, *a, **k: int(pid) == 222):
            instances = runtime.lire_instances()
        self.assertEqual(len(instances), 1)
        self.assertTrue(fiche.exists())

    def test_bug8_json_corrompu_n_est_plus_supprime(self):
        """Revue du 27/08, bug 8 : une fiche présente mais illisible (JSON
        partiel, corruption) était supprimée sur le seul JSONDecodeError,
        faisant perdre à stop la trace d'un processus peut-être toujours
        vivant. Doit maintenant être laissée en l'état pour cette lecture."""
        dossier = self.home / runtime.INSTANCES
        dossier.mkdir()
        fiche = dossier / "111.json"
        fiche.write_text('{"pid": 111, "verbes": [["serve"]]', encoding="utf-8")
        instances = runtime.lire_instances()
        self.assertEqual(instances, [])
        self.assertTrue(fiche.exists(), "une fiche corrompue ne doit plus être détruite")

    def test_bug8_ecriture_de_fiche_est_atomique(self):
        """Revue du 27/08, bug 8 : inscrire_instance()/inscrire_travailleur()
        écrivaient directement sur la fiche, une lecture concurrente pouvant
        y voir du JSON tronqué. Le fichier temporaire ne doit plus traîner
        une fois l'écriture terminée, preuve que le renommage a eu lieu."""
        fiche = runtime.inscrire_instance([["serve"]])
        try:
            self.assertEqual(
                json.loads(fiche.read_text(encoding="utf-8"))["pid"], os.getpid())
            self.assertEqual(list(fiche.parent.glob("*.tmp")), [])
        finally:
            fiche.unlink(missing_ok=True)

    def test_I14_echec_arret_conserve_la_fiche(self):
        """I-14 : stop ne doit pas perdre la seule piste d'un survivant."""
        fiche = self.home / "instance.json"
        fiche.write_text("{}", encoding="utf-8")
        instance = {"pid": 111, "depuis": "maintenant", "verbes": [["serve"]],
                    "enfants": [222], "fiche": fiche}
        with mock.patch.object(runtime, "lire_instances", return_value=[instance]), \
             mock.patch.object(runtime, "arreter_processus"), \
             mock.patch.object(runtime, "processus_vivant", return_value=True), \
             mock.patch.object(runtime, "processus_correspond", return_value=True), \
             contextlib.redirect_stdout(io.StringIO()):
            code = blink_cli.arreter([])
        self.assertEqual(code, 1)
        self.assertTrue(fiche.exists())

    def test_stop_ne_tue_pas_un_pid_reattribue(self):
        """Un numéro de PID existant mais réattribué à un autre logiciel ne
        doit jamais être tué : ça a réellement arrêté un service tiers et une
        messagerie sur la machine d'un utilisateur (numéro recyclé après la
        mort du vrai processus suivi). `processus_vivant` seul ne suffit pas
        à confirmer une identité ; `arreter` ne doit tuer que ce que
        `processus_correspond` reconnaît vraiment."""
        fiche = self.home / "instance.json"
        fiche.write_text("{}", encoding="utf-8")
        instance = {"pid": 111, "depuis": "maintenant", "verbes": [["serve"]],
                    "enfants": [], "fiche": fiche}
        # Horloge et sommeil liés : processus_vivant figé sur True fait
        # durer le délai de grâce coopératif (revue du 27/08) tout son
        # cours réel (15 s) si le temps n'est pas aussi accéléré ici - hors
        # sujet pour ce test, qui porte sur l'identité, pas le délai.
        horloge = {"t": 0.0}
        with mock.patch.object(runtime, "lire_instances", return_value=[instance]), \
             mock.patch.object(runtime, "arreter_processus") as tuer, \
             mock.patch.object(runtime, "processus_vivant", return_value=True), \
             mock.patch.object(runtime, "ligne_de_commande",
                                return_value=r"C:\Program Files\Autre\Logiciel.exe"), \
             mock.patch("time.time", side_effect=lambda: horloge["t"]), \
             mock.patch("time.sleep", side_effect=lambda d: horloge.__setitem__("t", horloge["t"] + d)), \
             contextlib.redirect_stdout(io.StringIO()):
            code = blink_cli.arreter([])
        # Le plus important : jamais un appel à tuer un processus dont
        # l'identité ne correspond pas, quel que soit le reste.
        tuer.assert_not_called()
        # Un PID réattribué n'est pas un survivant à retenir : notre
        # processus, lui, est bel et bien mort (identité non reconnue), la
        # fiche périmée part comme si l'arrêt avait réussi.
        self.assertEqual(code, 0)
        self.assertFalse(fiche.exists())

    def test_stop_tue_le_travailleur_ffmpeg_de_la_fusion(self):
        """Un ffmpeg de fusion (merge_daily.run_ffmpeg_batch/concat_copy) est
        un vrai enfant du PID principal, mais celui-ci n'a jamais droit au
        taskkill /T (protection du navigateur, voir arreter_processus) : sans
        inscription dédiée, ffmpeg restait orphelin et tournait jusqu'à sa fin
        après « stop » (signalé sur Reddit, 2026-08-26). Sa ligne de commande
        ne porte jamais « blink2video » : il lui faut sa propre empreinte,
        jamais celle par défaut du projet."""
        fiche = self.home / "instance.json"
        fiche.write_text("{}", encoding="utf-8")
        instance = {"pid": 111, "depuis": "maintenant", "verbes": [["watch", "--loop"]],
                    "enfants": [], "travailleurs": [333], "fiche": fiche}

        def correspond(pid, marqueurs=None):
            if int(pid) == 333:
                return marqueurs == ["ffmpeg"]
            return True

        # Horloge et sommeil liés : processus_vivant figé sur True fait
        # durer le délai de grâce coopératif (revue du 27/08) tout son
        # cours réel (15 s) si le temps n'est pas aussi accéléré ici -
        # hors sujet pour ce test, qui porte sur l'empreinte du travailleur.
        horloge = {"t": 0.0}

        def faux_temps():
            return horloge["t"]

        def faux_sommeil(duree):
            horloge["t"] += duree

        with mock.patch.object(runtime, "lire_instances", return_value=[instance]), \
             mock.patch.object(runtime, "arreter_processus") as tuer, \
             mock.patch.object(runtime, "processus_vivant", return_value=True), \
             mock.patch.object(runtime, "processus_correspond", side_effect=correspond), \
             mock.patch("time.time", side_effect=faux_temps), \
             mock.patch("time.sleep", side_effect=faux_sommeil), \
             contextlib.redirect_stdout(io.StringIO()):
            code = blink_cli.arreter([])
        tuer.assert_any_call(333, avec_descendance=True)
        # processus_correspond figé sur True (comme I-14 ci-dessus) : le
        # membre « survit » toujours à sa propre vérification post-arrêt,
        # sans lien avec le mock de arreter_processus. Ce que ce test vérifie
        # est en amont, la ligne assert_any_call : le travailleur n'est tué
        # qu'après avoir passé sa propre empreinte, jamais celle du projet.
        self.assertEqual(code, 1)
        self.assertTrue(fiche.exists())

    def test_maj_annonce_le_bon_dossier_de_donnees_a_la_relance(self):
        """Bug corrige le 27 aout 2026 (signale sur Reddit) : pendant une
        mise a jour, BLINK_HOME etait force sur le dossier d'installation
        lui-meme pour la version relancee, ramenant le dossier de donnees a
        celui de l'executable meme quand l'utilisateur l'avait explicitement
        redirige ailleurs via le panneau de reglages. installer() doit
        annoncer le dossier reellement en vigueur (pointeur suivi), pas le
        dossier d'installation brut."""
        installe = self.racine / "installe"
        installe.mkdir()
        (installe / "blink2video.exe").write_text("", encoding="utf-8")
        reel = self.racine / "stockage_redirige"
        (installe / runtime.POINTEUR_STOCKAGE).write_text(str(reel), encoding="utf-8")

        neuve = {
            "version": "9.9.9",
            "archive": {"nom": "archive.zip",
                        "url": "https://example.invalid/archive.zip",
                        "taille": 10},
        }
        dossier_extrait = self.racine / "extrait"
        dossier_extrait.mkdir()

        with mock.patch.object(runtime, "build_windows7", return_value=False), \
             mock.patch.object(runtime, "frozen", return_value=True), \
             mock.patch("sys.executable", str(installe / "blink2video.exe")), \
             mock.patch.object(maj, "disponible", return_value=neuve), \
             mock.patch.object(maj, "_telecharger"), \
             mock.patch.object(maj, "_extraire", return_value=dossier_extrait), \
             mock.patch.object(maj, "_rendre_executable"), \
             mock.patch.object(maj, "_verifier", return_value=True), \
             mock.patch.object(runtime, "demarrer") as demarrer, \
             contextlib.redirect_stdout(io.StringIO()):
            code = maj.installer()

        self.assertEqual(code, 0)
        demarrer.assert_called_once()
        env_passe = demarrer.call_args.kwargs["env"]
        # demarrer() est simulé : le vrai fichier maj.log qu'installer() a
        # ouvert pour son stdout ne serait jamais fermé par un Popen réel.
        # tearDown() efface ce dossier temporaire juste après ce test :
        # sous Windows, le fichier encore ouvert bloquerait ce nettoyage.
        demarrer.call_args.kwargs["stdout"].close()
        # Le point du bug : BLINK_HOME ne doit jamais valoir `installe` tel
        # quel des lors qu'un pointeur y redirige le stockage.
        self.assertEqual(env_passe["BLINK_HOME"], str(reel.resolve()))
        self.assertNotEqual(env_passe["BLINK_HOME"], str(installe))

    def test_E01_sans_argument_selectionne_start(self):
        """E-01/5.1 : l'absence d'argument emprunte le même dispatch que
        « start », préflight compris (blink2video.route, pas parse_args)."""
        with mock.patch.object(blink_cli, "executer") as executer_simule:
            blink_cli.route([])
        executer_simule.assert_called_once_with(
            runtime.decouper_verbes(["start"]))

    def test_E01_aide_et_version_ne_passent_pas_par_start(self):
        """5.2 : --help et --version gardent leur sens, aucun préflight."""
        with mock.patch.object(blink_cli, "executer") as executer_simule, \
             mock.patch.object(sys, "argv", ["blink2video", "--help"]), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as capture:
                blink_cli.route(["--help"])
        executer_simule.assert_not_called()
        self.assertEqual(capture.exception.code, 0)

    def test_B03_renumerotation_utilise_le_chemin_memorise(self):
        """B-03 : un ID distant nouveau ne retélécharge pas le même événement."""
        sortie = self.home / "clips"
        sync = FauxSync()
        ancien = FauxClip(42)
        nouveau = FauxClip(99)
        ancienne_cible = blink_models.target_path(sortie, ancien)
        ancienne_cible.parent.mkdir(parents=True)
        ancienne_cible.write_bytes(b"video-originale")
        etat = {"version": 1, "clips": {}}
        blink_registre.remember_download(etat, sync, "Test", ancien, sortie, ancienne_cible)
        nouvelle_cible = blink_models.target_path(sortie, nouveau)
        self.assertEqual(blink_registre.state_key(sync, ancien), blink_registre.state_key(sync, nouveau))
        self.assertTrue(blink_registre.is_downloaded(etat, sync, nouveau, nouvelle_cible))

    def test_B04_fusion_perimee_preserve_une_exclusion_plus_recente(self):
        """B-04 : une copie périmée ne peut retirer une tombstone."""
        sortie = self.home / "clips"
        sortie.mkdir()
        fichier = sortie / blink_registre.STATE_FILENAME
        # created_at valide requis : sans lui, load_download_state() écarte
        # l'entrée « commun » avant même la fusion, ce qui faisait échouer ce
        # test pour une raison étrangère à B-04 (fixture invalide).
        fichier.write_text(json.dumps({
            "version": 1,
            "clips": {"commun": {
                "path": "x.mp4", "excluded": True,
                "created_at": "2026-08-13T12:00:00+00:00",
            }},
        }), encoding="utf-8")
        perime = {"version": 1, "clips": {
            "commun": {"path": "x.mp4", "created_at": "2026-08-13T12:00:00+00:00"},
            "nouveau": {"path": "y.mp4", "created_at": "2026-08-13T12:05:00+00:00"},
        }}
        blink_registre._ecrire_registre(fichier, perime)
        fusion = json.loads(fichier.read_text(encoding="utf-8"))
        self.assertIs(fusion["clips"]["commun"].get("excluded"), True)

    def test_bug2_reintegration_survit_a_un_downloader_perime(self):
        """Revue du 27/08, bug 2 : contrepoint de B-04. Un clip vient d'être
        réintégré (excluded=False, horodaté) ; un downloader qui tenait
        encore une copie excluded=True lue avant cette réintégration ne doit
        pas la défaire en écrivant après coup - seule la décision la plus
        récente doit compter, dans les deux sens."""
        sortie = self.home / "clips"
        sortie.mkdir()
        fichier = sortie / blink_registre.STATE_FILENAME
        fichier.write_text(json.dumps({
            "version": 1,
            "clips": {"commun": {
                "path": "x.mp4", "excluded": False,
                "excluded_at": "2026-08-27T12:00:10+00:00",
                "created_at": "2026-08-13T12:00:00+00:00",
            }},
        }), encoding="utf-8")
        perime = {"version": 1, "clips": {
            "commun": {
                "path": "x.mp4", "excluded": True,
                "excluded_at": "2026-08-27T12:00:00+00:00",
                "created_at": "2026-08-13T12:00:00+00:00",
            },
        }}
        blink_registre._ecrire_registre(fichier, perime)
        fusion = json.loads(fichier.read_text(encoding="utf-8"))
        self.assertIs(fusion["clips"]["commun"].get("excluded"), False)

    def test_B05_un_verrou_vivant_ne_perime_pas_sur_le_seul_ttl(self):
        """B-05 : le TTL ne permet pas de voler un propriétaire vivant."""
        verrou = self.home / ".blink_ttl.lock"
        verrou.write_text(json.dumps({
            "owner": "travail-long", "pid": os.getpid(), "at": time.time() - 120,
        }), encoding="utf-8")
        with self.assertRaises(runtime.BusyError):
            with runtime.verrou("ttl", "challenger", stale_after=60):
                pass

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

    async def verifier_http_refuse(self, statut: int) -> None:
        """Exerce le chemin cloud complet, jusqu'au registre incrémental."""
        sortie = self.home / f"clips-{statut}"
        clip = blink_models.CloudClip({
            "id": statut,
            "device_name": "jardin",
            "created_at": "2026-08-13T12:00:00+00:00",
            "media": f"https://example.invalid/{statut}",
            "network_id": 7,
        })
        cible = blink_models.target_path(sortie, clip)
        blink = FauxBlinkHTTP(FauxReponse(statut, b"<html>erreur</html>"))
        with mock.patch.object(
            blink_models, "read_cloud_manifest", new=mock.AsyncMock(return_value=[clip]),
        ), contextlib.redirect_stdout(io.StringIO()):
            await blink_engine.traiter_cloud(
                blink,
                self.arguments(command="download", source="cloud", output=sortie),
                [],
            )
        self.assertFalse(cible.exists())
        self.assertFalse(cible.with_suffix(cible.suffix + ".part").exists())
        self.assertFalse((sortie / blink_registre.STATE_FILENAME).exists())

    async def test_B01_cloud_vide_retourne_un_tuple(self):
        """B-01 : un inventaire vide renvoie les quatre compteurs à zéro."""
        with mock.patch.object(blink_models, "read_cloud_manifest",
                               new=mock.AsyncMock(return_value=[])):
            resultat = await blink_engine.traiter_cloud(object(), self.arguments(), [])
        self.assertIsInstance(resultat, tuple)
        self.assertEqual(
            (resultat.downloaded, resultat.adopted, resultat.skipped, resultat.failed),
            (0, 0, 0, 0),
        )

    async def test_B01_filtre_camera_vide_retourne_un_tuple(self):
        """B-01 : un filtre sans résultat conserve les quatre compteurs."""
        with mock.patch.object(blink_models, "read_cloud_manifest",
                               new=mock.AsyncMock(return_value=[FauxClip()] )):
            resultat = await blink_engine.traiter_cloud(
                object(), self.arguments(camera="absente"), [],
            )
        self.assertIsInstance(resultat, tuple)
        self.assertEqual(
            (resultat.downloaded, resultat.adopted, resultat.skipped, resultat.failed),
            (0, 0, 0, 0),
        )

    async def test_B01_cloud_vide_un_passage_retourne_code_zero(self):
        """B-01 : l'appelant peut déballer le succès vide sans ``TypeError``."""
        with mock.patch.object(
            blink_models, "read_cloud_manifest", new=mock.AsyncMock(return_value=[]),
        ), contextlib.redirect_stdout(io.StringIO()):
            code = await blink_engine.un_passage(
                object(), self.arguments(command="list", source="cloud"), [],
            )
        self.assertEqual(code, 0)

    async def test_B01_filtre_vide_un_passage_retourne_code_zero(self):
        """B-01 : le filtre vide garde le code de succès de ``list``."""
        with mock.patch.object(
            blink_models, "read_cloud_manifest", new=mock.AsyncMock(return_value=[FauxClip()]),
        ), contextlib.redirect_stdout(io.StringIO()):
            code = await blink_engine.un_passage(
                object(),
                self.arguments(command="list", source="cloud", camera="absente"),
                [],
            )
        self.assertEqual(code, 0)

    async def test_B02_http_404_non_vide_ne_cree_ni_cible_ni_registre(self):
        """B-02 : une page 404 ne devient jamais un MP4 acquis."""
        await self.verifier_http_refuse(404)

    async def test_B02_http_429_non_vide_ne_cree_ni_cible_ni_registre(self):
        """B-02 : le corps d'un rate-limit reste une erreur transitoire."""
        await self.verifier_http_refuse(429)

    async def test_B02_http_500_non_vide_ne_cree_ni_cible_ni_registre(self):
        """B-02 : une erreur serveur non vide n'est pas enregistrée."""
        await self.verifier_http_refuse(500)

    async def test_I15_http_200_corps_non_mp4_ne_cree_pas_de_cible(self):
        """I-15 : un statut 2xx ne suffit pas, le corps doit être un MP4
        réel (boîte ftyp) ; un octet non nul ne l'est pas forcément."""
        sortie = self.home / "clips-200-invalide"
        clip = blink_models.CloudClip({
            "id": 1, "device_name": "jardin",
            "created_at": "2026-08-13T12:00:00+00:00",
            "media": "https://example.invalid/200", "network_id": 7,
        })
        cible = blink_models.target_path(sortie, clip)
        blink = FauxBlinkHTTP(FauxReponse(200, b"<html>pas un mp4</html>"))
        with mock.patch.object(
            blink_models, "read_cloud_manifest", new=mock.AsyncMock(return_value=[clip]),
        ), contextlib.redirect_stdout(io.StringIO()):
            await blink_engine.traiter_cloud(
                blink,
                self.arguments(command="download", source="cloud", output=sortie),
                [],
            )
        self.assertFalse(cible.exists())
        self.assertFalse((sortie / blink_registre.STATE_FILENAME).exists())

    async def test_I15_http_200_corps_mp4_valide_est_acquis(self):
        """Non-régression : un vrai MP4 (boîte ftyp) reste accepté."""
        sortie = self.home / "clips-200-valide"
        clip = blink_models.CloudClip({
            "id": 2, "device_name": "jardin",
            "created_at": "2026-08-13T12:00:00+00:00",
            "media": "https://example.invalid/200-ok", "network_id": 7,
        })
        cible = blink_models.target_path(sortie, clip)
        corps = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100
        blink = FauxBlinkHTTP(FauxReponse(200, corps))
        with mock.patch.object(
            blink_models, "read_cloud_manifest", new=mock.AsyncMock(return_value=[clip]),
        ), contextlib.redirect_stdout(io.StringIO()):
            await blink_engine.traiter_cloud(
                blink,
                self.arguments(command="download", source="cloud", output=sortie),
                [],
            )
        self.assertTrue(cible.is_file())

    async def test_I18_compteurs_cloud_separent_les_quatre_issues(self):
        """I-18 : téléchargé, adopté, ignoré et échoué restent distinguables."""
        sortie = self.home / "compteurs"
        sortie.mkdir()
        instant = dt.datetime(2026, 8, 13, 12, tzinfo=dt.timezone.utc)
        saute = FauxClip(1, "deja-acquis", instant)
        adopte = FauxClip(2, "a-adopter", instant + dt.timedelta(minutes=1))
        telecharge = FauxClip(3, "a-telecharger", instant + dt.timedelta(minutes=2))
        echoue = FauxClip(4, "en-echec", instant + dt.timedelta(minutes=3))

        async def ne_doit_pas_telecharger(_blink, _cible):
            raise AssertionError("un clip acquis ou adopté ne doit pas être téléchargé")

        async def telecharger(_blink, cible):
            cible.write_bytes(b"video-neuve")
            return True

        async def refuser(_blink, cible):
            cible.write_bytes(b"reponse-partielle")
            return False

        saute.download_to = ne_doit_pas_telecharger
        adopte.download_to = ne_doit_pas_telecharger
        telecharge.download_to = telecharger
        echoue.download_to = refuser

        cible_sautee = blink_models.target_path(sortie, saute)
        cible_sautee.parent.mkdir(parents=True)
        cible_sautee.write_bytes(b"video-connue")
        cible_adoptee = blink_models.target_path(sortie, adopte)
        cible_adoptee.parent.mkdir(parents=True)
        # Un vrai MP4 (en-tête ftyp, >= 64 octets) : depuis la revue de code
        # du 0eab463 (bug #4), l'adoption exige valid_mp4(), pas seulement
        # une taille non nulle - ce test vérifie l'adoption d'un fichier
        # réellement valide, pas le trou que le bug laissait passer.
        cible_adoptee.write_bytes(b"    ftyp" + b"\x00" * 56)

        etat = {"version": 1, "clips": {}}
        blink_registre.remember_download(etat, FauxSync(10), "cloud", saute, sortie, cible_sautee,
                              source="cloud")
        (sortie / blink_registre.STATE_FILENAME).write_text(
            json.dumps(etat), encoding="utf-8",
        )

        with mock.patch.object(
            blink_models,
            "read_cloud_manifest",
            new=mock.AsyncMock(return_value=[saute, adopte, telecharge, echoue]),
        ), contextlib.redirect_stdout(io.StringIO()):
            resultat = await blink_engine.traiter_cloud(
                object(),
                self.arguments(command="download", source="cloud", output=sortie),
                [("Maison", FauxSync(10))],
            )

        self.assertEqual(
            (resultat.downloaded, resultat.adopted, resultat.skipped, resultat.failed),
            (1, 1, 1, 1),
        )
        self.assertTrue(blink_models.target_path(sortie, telecharge).exists())
        self.assertFalse(blink_models.target_path(sortie, echoue).exists())
        self.assertFalse(
            blink_models.target_path(sortie, echoue).with_suffix(".mp4.part").exists(),
        )
        registre = blink_registre.load_download_state(sortie)
        self.assertEqual(len(registre["clips"]), 3)

    async def test_I18_fichier_existant_invalide_n_est_pas_adopte(self):
        """Bug #4, revue de code du 0eab463 : un fichier présent au bon
        chemin mais pas un MP4 valide (texte, écriture interrompue...) ne
        doit pas être « adopté » sans être re-téléchargé, sous prétexte
        qu'il n'est pas vide. Seule md.valid_mp4() doit trancher, pas
        st_size > 0."""
        sortie = self.home / "invalide"
        sortie.mkdir()
        instant = dt.datetime(2026, 8, 13, 12, tzinfo=dt.timezone.utc)
        corrompu = FauxClip(1, "pas-un-mp4", instant)

        async def retelecharger(_blink, cible):
            cible.write_bytes(b"    ftyp" + b"\x00" * 56)
            return True

        corrompu.download_to = retelecharger

        cible = blink_models.target_path(sortie, corrompu)
        cible.parent.mkdir(parents=True)
        cible.write_bytes(b"ceci n'est pas un mp4")

        with mock.patch.object(
            blink_models, "read_cloud_manifest",
            new=mock.AsyncMock(return_value=[corrompu]),
        ), contextlib.redirect_stdout(io.StringIO()):
            resultat = await blink_engine.traiter_cloud(
                object(),
                self.arguments(command="download", source="cloud", output=sortie),
                [("Maison", FauxSync(10))],
            )

        self.assertEqual(resultat.adopted, 0)
        self.assertEqual(resultat.downloaded, 1)
        self.assertTrue(blink_engine.md.valid_mp4(cible))

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
        (sortie / blink_registre.STATE_FILENAME).write_text(json.dumps({
            "version": 1,
            "clips": {"ancien": {
                "camera": clip.name,
                "created_at": clip.created_at.isoformat(),
                "path": "fichier-disparu.mp4",
                "source": "cloud",
            }},
        }), encoding="utf-8")
        with mock.patch.object(blink_models, "read_cloud_manifest",
                               new=mock.AsyncMock(return_value=[clip])):
            with contextlib.redirect_stdout(io.StringIO()):
                resultat = await blink_engine.traiter_cloud(
                    object(), self.arguments(command="download", output=sortie), [],
                )
        self.assertEqual(resultat, blink_engine.CloudResult(1, 0, 0, 0))

    async def test_I09_compte_cloud_only_accepte_login(self):
        """I-09 : login ne dépend pas de la présence d'un Sync Module."""
        blink = SimpleNamespace(sync={})
        with mock.patch("aiohttp.ClientSession", FauxSessionHTTP), \
             mock.patch.object(blink_auth, "connect", new=mock.AsyncMock(return_value=blink)), \
             contextlib.redirect_stdout(io.StringIO()):
            code = await blink_cli.main(self.arguments(command="login"))
        self.assertEqual(code, 0)

    async def test_I09_compte_sans_hub_accepte_la_source_cloud(self):
        """I-09 : l'absence de Sync Module n'interdit pas le cloud du compte."""
        blink = SimpleNamespace(sync={})
        arguments = self.arguments(command="list", source="cloud")
        boucle = mock.AsyncMock(return_value=0)
        with mock.patch("aiohttp.ClientSession", FauxSessionHTTP), \
             mock.patch.object(blink_auth, "connect", new=mock.AsyncMock(return_value=blink)), \
             mock.patch.object(blink_engine, "boucler", new=boucle), \
             contextlib.redirect_stdout(io.StringIO()):
            code = await blink_cli.main(arguments)
        self.assertEqual(code, 0)
        boucle.assert_awaited_once_with(blink, arguments, [])

    async def test_I16_noms_distincts_ne_collisionnent_pas_apres_assainissement(self):
        """I-16 : safe_name sert au chemin, jamais à l'identité métier."""
        local = FauxClip(1, nom="Entrée/jardin")
        cloud = FauxClip(2, nom="Entrée_jardin")
        inedits, doublons = blink_models.rapprocher([local], [cloud])
        self.assertEqual([clip.id for clip in inedits], [2])
        self.assertEqual(doublons, [])

    async def test_B06_usb_prend_le_verrou_du_hub(self):
        """B-06 : manifeste et téléchargement USB réservent le Sync Module."""
        noms = []

        @contextlib.contextmanager
        def verrou_trace(nom, *_args, **_kwargs):
            noms.append(nom)
            yield

        sync = FauxSync()
        with mock.patch.object(runtime, "verrou", side_effect=verrou_trace), \
             mock.patch.object(blink_models, "read_local_manifest",
                               new=mock.AsyncMock(return_value=[FauxClip()])), \
             mock.patch.object(blink_engine, "download_clip",
                               new=mock.AsyncMock(return_value="failed")), \
             mock.patch.object(blink_registre, "save_download_state"), \
             mock.patch.object(runtime, "travail"), \
             contextlib.redirect_stdout(io.StringIO()):
            await blink_engine.un_passage(
                object(), self.arguments(command="download", source="usb"),
                [("Test", sync)],
            )
        self.assertIn("hub", noms)

    async def test_B06_module_occupe_par_le_direct_n_abat_pas_le_passage(self):
        """B-06 : le direct détient déjà le verrou hub (serve.py) ; le tour
        USB doit le signaler et continuer, jamais planter la boucle (I-17)."""
        sync = FauxSync()
        with mock.patch.object(
            runtime, "verrou",
            side_effect=runtime.BusyError("détenu par « direct » depuis 12 s"),
        ), mock.patch.object(
            blink_models, "read_local_manifest",
            new=mock.AsyncMock(return_value=[FauxClip()]),
        ), contextlib.redirect_stdout(io.StringIO()) as sortie:
            code = await blink_engine.un_passage(
                object(), self.arguments(command="download", source="usb"),
                [("Test", sync)],
            )
        self.assertEqual(code, 1)
        self.assertIn("occupé", sortie.getvalue())

    async def test_B06_cloud_n_est_jamais_verrouille_par_le_hub(self):
        """Le cloud répond depuis le compte, pas le module : aucune réserve
        du hub ne doit lui être appliquée (indépendance de 6.5)."""
        noms = []

        @contextlib.contextmanager
        def verrou_trace(nom, *_args, **_kwargs):
            noms.append(nom)
            yield

        with mock.patch.object(runtime, "verrou", side_effect=verrou_trace), \
             mock.patch.object(blink_models, "read_cloud_manifest",
                               new=mock.AsyncMock(return_value=[])), \
             contextlib.redirect_stdout(io.StringIO()):
            await blink_engine.un_passage(
                object(), self.arguments(command="download", source="cloud"),
                [],
            )
        self.assertNotIn("hub", noms)

    async def test_I10_entree_de_registre_non_objet_n_abat_pas_le_cloud(self):
        """I-10 : une entrée corrompue est isolée du reste du registre."""
        sortie = self.home / "clips"
        sortie.mkdir()
        (sortie / blink_registre.STATE_FILENAME).write_text(json.dumps({
            "version": 1,
            "clips": {
                "nombre": 17,
                "nul": None,
                "tableau": [],
                "sans_date": {"camera": "jardin"},
                "date_invalide": {"camera": "jardin", "created_at": "jamais"},
            },
        }), encoding="utf-8")
        with mock.patch.object(blink_models, "read_cloud_manifest",
                               new=mock.AsyncMock(return_value=[FauxClip()])), \
             contextlib.redirect_stdout(io.StringIO()):
            resultat = await blink_engine.traiter_cloud(
                object(), self.arguments(output=sortie), [],
            )
        self.assertEqual(
            (resultat.downloaded, resultat.adopted, resultat.skipped, resultat.failed),
            (0, 0, 0, 0),
        )

    async def test_I07_depuis_zero_signifie_aujourd_hui(self):
        """I-07 : `--since 0` ne doit pas se transformer en trente jours."""
        appels = []

        class Blink:
            async def get_videos_metadata(self, **options):
                appels.append(options)
                return []

        with mock.patch.object(blink_models.dt, "datetime", DateHeureFigee):
            await blink_models.read_cloud_manifest(Blink(), 0)
        self.assertEqual(appels[0]["since"], "2026/08/13 12:34:56")

    async def test_I07_depuis_none_utilise_la_fenetre_par_defaut(self):
        """I-07 : l'absence de filtre conserve explicitement les trente jours."""
        appels = []

        class Blink:
            async def get_videos_metadata(self, **options):
                appels.append(options)
                return []

        with mock.patch.object(blink_models.dt, "datetime", DateHeureFigee):
            await blink_models.read_cloud_manifest(Blink(), None)
        self.assertEqual(appels[0]["since"], "2026/07/14 12:34:56")

    async def test_I07_depuis_n_utilise_exactement_n_jours(self):
        """I-07 : une valeur positive n'est ni remplacée ni décalée."""
        appels = []

        class Blink:
            async def get_videos_metadata(self, **options):
                appels.append(options)
                return []

        with mock.patch.object(blink_models.dt, "datetime", DateHeureFigee):
            await blink_models.read_cloud_manifest(Blink(), 7)
        self.assertEqual(appels[0]["since"], "2026/08/06 12:34:56")

    async def test_I06_plafond_cloud_releve_a_400_pages(self):
        """Bug #6, revue de code du 0eab463 : stop=20 (19 pages, ~475 clips)
        tronquait en silence un compte actif ; le plafond est desormais
        PLAFOND_PAGES_CLOUD (400), passe tel quel a blinkpy."""
        appels = []

        class Blink:
            async def get_videos_metadata(self, **options):
                appels.append(options)
                return []

        with mock.patch.object(blink_models.dt, "datetime", DateHeureFigee):
            await blink_models.read_cloud_manifest(Blink(), None)
        self.assertEqual(appels[0]["stop"], blink_models.PLAFOND_PAGES_CLOUD)
        self.assertGreater(blink_models.PLAFOND_PAGES_CLOUD, 20)

    async def test_I06_avertit_quand_proche_du_plafond(self):
        """Bug #6 : impossible de savoir depuis ici si la pagination s'est
        arretee sur une page vide (fin reelle) ou sur le plafond (troncature
        possible) - un volume proche du maximum theorique doit se signaler."""
        volume = (blink_models.PLAFOND_PAGES_CLOUD - 3) * 20

        class BlinkPlein:
            async def get_videos_metadata(self, **options):
                return [{"deleted": True} for _ in range(volume)]

        with mock.patch.object(blink_models.dt, "datetime", DateHeureFigee), \
             contextlib.redirect_stdout(io.StringIO()) as sortie:
            await blink_models.read_cloud_manifest(BlinkPlein(), None)
        self.assertIn("plafond", sortie.getvalue())

    async def test_I06_pas_d_avertissement_sous_le_plafond(self):
        class BlinkNormal:
            async def get_videos_metadata(self, **options):
                return [{"deleted": True} for _ in range(30)]

        with mock.patch.object(blink_models.dt, "datetime", DateHeureFigee), \
             contextlib.redirect_stdout(io.StringIO()) as sortie:
            await blink_models.read_cloud_manifest(BlinkNormal(), None)
        self.assertNotIn("plafond", sortie.getvalue())

    async def test_I17_boucler_survit_a_une_erreur_de_tour(self):
        """I-17 : seul BusyError était intercepté ; toute autre erreur (HTTP,
        auth, schéma) tuait définitivement le worker de fond."""
        appels = []

        async def un_passage_instable(blink, args, modules):
            appels.append(len(appels))
            if len(appels) == 1:
                raise ConnectionError("panne réseau simulée")
            args.loop = None  # deuxième tour : on arrête proprement la boucle
            return 0

        with mock.patch.object(blink_engine, "un_passage", new=un_passage_instable), \
             mock.patch.object(
                 blink_engine, "_attendre_echeance",
                 new=mock.AsyncMock(return_value=True),
             ), \
             contextlib.redirect_stdout(io.StringIO()):
            code = await blink_engine.boucler(object(), self.arguments(loop=1), [])

        self.assertEqual(code, 0)
        self.assertEqual(len(appels), 2)

    async def test_O05_boucler_calcule_l_echeance_depuis_le_debut_du_tour(self):
        """O-05 : la pause du prochain tour doit tenir compte du temps déjà
        passé dans le tour courant, pas repartir de zéro après le travail."""
        horloge = {"t": 1_000.0}
        durees = []
        appels = []

        async def un_passage_lent(blink, args, modules):
            appels.append(None)
            horloge["t"] += 15.0  # le tour "dure" 15 s
            if len(appels) >= 2:
                args.loop = None  # on arrête après avoir observé une pause
            return 0

        async def attendre(echeance):
            durees.append(echeance - horloge["t"])
            horloge["t"] = echeance
            return True

        with mock.patch.object(blink_engine, "un_passage", new=un_passage_lent), \
             mock.patch.object(b2v.time, "monotonic", side_effect=lambda: horloge["t"]), \
             mock.patch.object(blink_engine, "_attendre_echeance", side_effect=attendre), \
             contextlib.redirect_stdout(io.StringIO()):
            await blink_engine.boucler(object(), self.arguments(loop=1), [])

        self.assertEqual(durees, [45.0])

    async def test_E01_preflight_sans_session_ne_touche_pas_le_reseau(self):
        """5.3/5.4 : sans fichier de session, aucun appel Blink n'est tenté.

        blink_auth.CONFIG doit être explicitement patché ici : sinon ce test lit et
        tente de rafraîchir le blink_auth.json réel de la machine, exactement
        ce que la section 12.5 interdit."""
        with mock.patch.object(blink_auth, "CONFIG", self.home / "blink_auth.json"):
            etat = await blink_auth.preflight()
        self.assertEqual(etat, {
            "authenticated": False, "networks": 0, "sync_modules": 0,
            "cameras": 0, "cloud_only": False, "error": None,
        })

    async def test_E01_preflight_mini_smoke_compte_cameras_et_reseaux(self):
        """5.12/5.13/5.14 : synthèse read-only, compte cloud-only accepté."""
        camera_a, camera_b = object(), object()
        sync_avec_module = SimpleNamespace(
            network_id=1, cameras={"jardin": camera_a, "terrasse": camera_b})
        blink = SimpleNamespace(sync={"Sync1": sync_avec_module})
        with mock.patch.object(blink_auth, "connect_saved",
                               new=mock.AsyncMock(return_value=blink)):
            etat = await blink_auth.preflight()
        self.assertEqual(etat["authenticated"], True)
        self.assertEqual(etat["cameras"], 2)
        self.assertEqual(etat["networks"], 1)
        self.assertEqual(etat["sync_modules"], 1)
        self.assertFalse(etat["cloud_only"])

    async def test_E01_preflight_compte_cloud_only_est_accepte(self):
        """5.14 : un compte valide sans Sync Module n'est pas une erreur."""
        blink = SimpleNamespace(sync={})
        with mock.patch.object(blink_auth, "connect_saved",
                               new=mock.AsyncMock(return_value=blink)):
            etat = await blink_auth.preflight()
        self.assertTrue(etat["authenticated"])
        self.assertTrue(etat["cloud_only"])
        self.assertEqual(etat["cameras"], 0)

    async def test_E01_preflight_capture_les_pannes_reseau(self):
        """Une panne réseau au préflight ne doit jamais faire planter start."""
        with mock.patch.object(blink_auth, "connect_saved",
                               new=mock.AsyncMock(side_effect=ConnectionError("hs"))):
            etat = await blink_auth.preflight()
        self.assertFalse(etat["authenticated"])
        self.assertIn("ConnectionError", etat["error"])

    async def test_livestream_readexactly_reassemble_payload_fragmente(self):
        """blinkpy #1232 (correctif amont pas encore publié) : un paquet
        coupé entre deux segments TCP ne doit pas être pris pour une
        connexion morte. ``read()`` rend dès la moindre miette disponible,
        ``readexactly()`` attend la taille annoncée par l'en-tête."""
        stream = blink_engine._blinkpy_livestream.BlinkLiveStream.__new__(
            blink_engine._blinkpy_livestream.BlinkLiveStream
        )
        entete = bytes([0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0xBC])
        charge = bytes([0x47] + [0x00] * 187)  # 188 octets, comme l'en-tête l'annonce

        lecteur = asyncio.StreamReader()
        lecteur.feed_data(entete)
        lecteur.feed_data(charge[:100])  # segment coupé : le reste arrive plus tard

        client = mock.Mock()
        client.is_closing.return_value = False
        client.write = mock.Mock()
        client.drain = mock.AsyncMock()

        stream.target_reader = lecteur
        stream.target_writer = mock.Mock()
        stream.clients = [client]

        tache = asyncio.ensure_future(stream.recv())
        await asyncio.sleep(0)  # laisse recv() vider le tampon et bloquer sur le reste

        lecteur.feed_data(charge[100:])
        lecteur.feed_eof()
        await tache

        client.write.assert_called_once_with(charge)


class FauxProcessusServe:
    """Simule le subprocess.Popen de « serve » lancé par accueillir()."""

    def __init__(self, vivant_pendant=None, code=1):
        self._tours = 0
        self.vivant_pendant = vivant_pendant
        self.returncode = None
        self._code = code
        self.pid = 4242
        self.terminated = False

    def poll(self):
        self._tours += 1
        if self.vivant_pendant is not None and self._tours > self.vivant_pendant:
            self.returncode = self._code
        return self.returncode


class TestsE01Onboarding(unittest.TestCase):
    """5.6 à 5.16 : onboarding minimal (version resserrée, sans CSRF ni
    rotation de port dédiées, voir AUDIT-2026-08-13.md section 28, étape 5)."""

    def setUp(self):
        self.etat_non_authentifie = {
            "authenticated": False, "networks": 0, "sync_modules": 0,
            "cameras": 0, "cloud_only": False, "error": None,
        }

    def test_E01_port_repond_puis_connexion_reussie(self):
        """5.10/5.12 : succès dès que le préflight redevient positif."""
        processus = FauxProcessusServe(vivant_pendant=None)
        succes = {"authenticated": True, "cameras": 3, "cloud_only": False}
        with mock.patch.object(b2v.runtime, "demarrer", return_value=processus), \
             mock.patch.object(blink_cli, "_port_ouvert", return_value=True), \
             mock.patch("webbrowser.open", return_value=True) as ouverture, \
             mock.patch.object(blink_auth, "preflight",
                               new=mock.AsyncMock(return_value=succes)), \
             mock.patch.dict(os.environ, {"BLINK_NO_BROWSER": "0"}), \
             contextlib.redirect_stdout(io.StringIO()):
            code = blink_cli.accueillir(self.etat_non_authentifie, [], delai=5)
        self.assertEqual(code, 0)
        ouverture.assert_called_once()
        self.assertTrue(str(ouverture.call_args[0][0]).endswith("/?login=1"))

    def test_E01_navigateur_absent_n_empeche_pas_le_flux(self):
        """5.9 : navigateur indisponible, le polling continue quand même."""
        processus = FauxProcessusServe(vivant_pendant=None)
        succes = {"authenticated": True, "cameras": 0, "cloud_only": True}
        with mock.patch.object(b2v.runtime, "demarrer", return_value=processus), \
             mock.patch.object(blink_cli, "_port_ouvert", return_value=True), \
             mock.patch("webbrowser.open", return_value=False), \
             mock.patch.object(blink_auth, "preflight",
                               new=mock.AsyncMock(return_value=succes)), \
             mock.patch.dict(os.environ, {"BLINK_NO_BROWSER": "0"}), \
             contextlib.redirect_stdout(io.StringIO()) as sortie:
            code = blink_cli.accueillir(self.etat_non_authentifie, [], delai=5)
        self.assertEqual(code, 0)
        self.assertIn("Navigateur indisponible", sortie.getvalue())

    def test_E01_delai_depasse_nettoie_le_serveur_temporaire(self):
        """5.11/5.16 : timeout, rien ne doit rester actif (Ctrl+C simulé par
        l'écoulement du délai plutôt qu'une vraie annulation manuelle)."""
        processus = FauxProcessusServe(vivant_pendant=None)
        jamais = mock.AsyncMock(return_value=self.etat_non_authentifie)
        with mock.patch.object(b2v.runtime, "demarrer", return_value=processus), \
             mock.patch.object(b2v.runtime, "arreter_processus") as arret, \
             mock.patch.object(blink_cli, "_port_ouvert", return_value=True), \
             mock.patch.object(blink_auth, "preflight", new=jamais), \
             mock.patch.dict(os.environ, {"BLINK_NO_BROWSER": "1"}), \
             contextlib.redirect_stdout(io.StringIO()) as sortie:
            code = blink_cli.accueillir(self.etat_non_authentifie, [], delai=0.05)
        self.assertEqual(code, 1)
        self.assertIn("Délai de connexion dépassé", sortie.getvalue())
        arret.assert_called_once_with(processus.pid, avec_descendance=True)

    def test_E01_serveur_mort_avant_le_port_est_signale(self):
        """Le processus « serve » temporaire meurt tout de suite (port pris
        par une autre application, par exemple) : abandon propre, sans
        attendre le délai complet."""
        processus = FauxProcessusServe(vivant_pendant=0, code=1)
        with mock.patch.object(b2v.runtime, "demarrer", return_value=processus), \
             mock.patch.object(b2v.runtime, "arreter_processus") as arret, \
             mock.patch.dict(os.environ, {"BLINK_NO_BROWSER": "1"}), \
             contextlib.redirect_stdout(io.StringIO()) as sortie:
            code = blink_cli.accueillir(self.etat_non_authentifie, [], delai=5)
        self.assertEqual(code, 1)
        self.assertIn("s'est arrêtée avant la connexion", sortie.getvalue())
        arret.assert_not_called()  # déjà mort : rien à arrêter en plus

    def test_E01_mini_smoke_precede_les_workers(self):
        """5.15/5.16 : échec de l'onboarding, aucune boucle de fond lancée."""
        # BLINK_HOME isole le verrou "start" (runtime.verrou) et le marqueur
        # de raccourci de bureau de l'installation réelle : sans ça, ce test
        # se met à dépendre de ce qui tourne déjà sur la machine (28.80/28.81
        # - le verrou "start" reste tenu tant que l'instance vit, pas
        # seulement le temps du démarrage).
        with tempfile.TemporaryDirectory() as domicile, \
             mock.patch.dict(os.environ, {"BLINK_HOME": domicile}), \
             mock.patch.object(blink_auth, "preflight",
                               new=mock.AsyncMock(return_value=self.etat_non_authentifie)), \
             mock.patch.object(blink_cli, "accueillir", return_value=1) as accueil, \
             mock.patch.object(blink_cli, "_port_ouvert", return_value=False), \
             mock.patch.object(b2v.runtime, "decouper_verbes") as decouper, \
             contextlib.redirect_stdout(io.StringIO()):
            code = blink_cli.executer([["start"]])
        self.assertEqual(code, 1)
        accueil.assert_called_once()
        decouper.assert_not_called()  # jamais atteint : pas de composition lancée

    def test_E01_session_et_configuration_valides_sautent_l_onboarding(self):
        """Une installation déjà configurée démarre directement (5.5)."""
        authentifie = dict(self.etat_non_authentifie, authenticated=True, cameras=2)
        with tempfile.TemporaryDirectory() as domicile, \
             mock.patch.dict(os.environ, {"BLINK_HOME": domicile}), \
             mock.patch.object(blink_auth, "preflight",
                               new=mock.AsyncMock(return_value=authentifie)), \
             mock.patch.object(blink_cli, "accueillir") as accueil, \
             mock.patch.object(blink_cli, "_port_ouvert", return_value=False), \
             mock.patch.object(blink_cli, "executer",
                               wraps=blink_cli.executer) as executer_espionne, \
             mock.patch.object(b2v.runtime, "demarrer") as demarrer, \
             mock.patch.object(tray, "disponible", return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            (Path(domicile) / runtime.MARQUEUR_CONFIGURATION_INITIALE).write_text(
                runtime.VERSION, encoding="utf-8")
            demarrer.return_value.pid = 4242
            blink_cli.executer([["start"]])
        accueil.assert_not_called()
        # La composition complète est bien tentée (au moins un appel demarrer).
        self.assertTrue(demarrer.called)
        self.assertGreaterEqual(executer_espionne.call_count, 1)

    def test_E01_configuration_initiale_refusee_precede_tous_les_workers(self):
        """Même authentifié, start ne compose rien avant « Appliquer »."""
        authentifie = dict(self.etat_non_authentifie, authenticated=True, cameras=2)
        with tempfile.TemporaryDirectory() as domicile, \
             mock.patch.dict(os.environ, {"BLINK_HOME": domicile}), \
             mock.patch.object(blink_auth, "preflight",
                               new=mock.AsyncMock(return_value=authentifie)), \
             mock.patch.object(blink_cli, "accueillir", return_value=1) as accueil, \
             mock.patch.object(blink_cli, "_port_ouvert", return_value=False), \
             mock.patch.object(b2v.runtime, "decouper_verbes") as decouper, \
             contextlib.redirect_stdout(io.StringIO()):
            (Path(domicile) / runtime.MARQUEUR_CONFIGURATION_EN_ATTENTE).write_text(
                runtime.VERSION, encoding="utf-8")
            code = blink_cli.executer([["start"]])
        self.assertEqual(code, 1)
        self.assertTrue(accueil.call_args.kwargs["configuration_initiale"])
        decouper.assert_not_called()

    def test_E01_serveur_initial_attend_le_marqueur_sans_workers(self):
        processus = FauxProcessusServe(vivant_pendant=None)
        authentifie = dict(self.etat_non_authentifie, authenticated=True, cameras=2)
        with mock.patch.object(b2v.runtime, "demarrer", return_value=processus) as demarrer, \
             mock.patch.object(b2v.runtime, "arreter_processus") as arret, \
             mock.patch.object(blink_cli, "_port_ouvert", return_value=True), \
             mock.patch.object(b2v.runtime, "configuration_initiale_effectuee",
                               side_effect=[False, True]), \
             mock.patch.object(blink_cli.time, "sleep"), \
             mock.patch("webbrowser.open", return_value=True) as ouverture, \
             mock.patch.dict(os.environ, {"BLINK_NO_BROWSER": "0"}), \
             contextlib.redirect_stdout(io.StringIO()):
            code = blink_cli.accueillir(
                authentifie, [], delai=5, configuration_initiale=True)
        self.assertEqual(code, 0)
        self.assertIn("--initial-setup", demarrer.call_args[0][0])
        self.assertTrue(str(ouverture.call_args[0][0]).endswith("/?setup=1"))
        arret.assert_called_once_with(processus.pid, avec_descendance=True)

    def test_raccourci_bureau_pose_une_seule_fois(self):
        """28.81 : le raccourci de bureau n'est proposé qu'au tout premier
        démarrage réussi, jamais aux suivants (marqueur sur disque)."""
        authentifie = dict(self.etat_non_authentifie, authenticated=True, cameras=2)
        with tempfile.TemporaryDirectory() as domicile, \
             mock.patch.dict(os.environ, {"BLINK_HOME": domicile}), \
             mock.patch.object(blink_auth, "preflight",
                               new=mock.AsyncMock(return_value=authentifie)), \
             mock.patch.object(blink_cli, "_port_ouvert", return_value=False), \
             mock.patch.object(b2v.runtime, "demarrer") as demarrer, \
             mock.patch.object(tray, "disponible", return_value=False), \
             mock.patch("raccourci_bureau.creer") as creer, \
             contextlib.redirect_stdout(io.StringIO()):
            (Path(domicile) / runtime.MARQUEUR_CONFIGURATION_INITIALE).write_text(
                runtime.VERSION, encoding="utf-8")
            demarrer.return_value.pid = 4242
            blink_cli.executer([["start"]])
            blink_cli.executer([["start"]])
        creer.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
