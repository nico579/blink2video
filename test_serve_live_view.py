"""Non-régression du direct MSE/MJPEG : lecture de tube avec un vrai délai
(AUDIT-2026-08-13.md, sections 28.22 et 28.26)."""

from __future__ import annotations

import asyncio
import io
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-live-view-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import serve  # noqa: E402 - bootstrap neutralisé avant import


def _boite(genre: bytes, charge: bytes) -> bytes:
    return (8 + len(charge)).to_bytes(4, "big") + genre + charge


def _segment_synthetique() -> bytes:
    """ftyp + moov > trak > mdia > minf > stbl > stsd > avc1 > avcC, avec
    profil/contraintes/niveau H.264 High (avc1.640028, vu en vrai en
    production)."""
    avcc = _boite(b"avcC", bytes([1, 0x64, 0x00, 0x28]) + bytes(16))
    avc1 = _boite(b"avc1", bytes(78) + avcc)
    stsd = _boite(b"stsd", bytes(8) + avc1)
    stbl = _boite(b"stbl", stsd)
    minf = _boite(b"minf", stbl)
    mdia = _boite(b"mdia", minf)
    trak = _boite(b"trak", mdia)
    moov = _boite(b"moov", trak)
    ftyp = _boite(b"ftyp", b"isom" + bytes(4))
    return ftyp + moov


class FauxPipeMorceaux:
    """Un tube qui ne rend ses octets qu'au compte-gouttes, comme ffmpeg le
    fait pour un moov qui déborde du premier bloc lu."""

    def __init__(self, morceaux: list):
        self._morceaux = list(morceaux)

    def read(self, _n: int) -> bytes:
        return self._morceaux.pop(0) if self._morceaux else b""


class FauxPipeLent:
    """Bloque plus longtemps que le délai testé avant de rendre un octet,
    comme ffmpeg qui se tait un moment sans jamais fermer son tube."""

    def __init__(self, attente: float, morceau: bytes):
        self._attente = attente
        self._morceau = morceau
        self._rendu = False

    def read(self, _n: int) -> bytes:
        if self._rendu:
            return b""
        time.sleep(self._attente)
        self._rendu = True
        return self._morceau


class FauxVerrou:
    def __init__(self, echec_sortie: bool = False):
        self.entre = False
        self.sorti = False
        self._echec_sortie = echec_sortie

    def __enter__(self):
        self.entre = True
        return self

    def __exit__(self, *_args):
        self.sorti = True
        if self._echec_sortie:
            raise OSError("verrou illisible")


class FauxProcessus:
    def __init__(self, sortie: bytes, erreurs: bytes = b""):
        self.stdout = FauxPipeMorceaux([sortie, b""])
        self.stderr = io.BytesIO(erreurs)
        self.termine = False
        self.attendu = False
        self.tue = False

    def terminate(self):
        self.termine = True

    def wait(self, timeout=None):
        self.attendu = True

    def kill(self):
        self.tue = True


class SortieDeconnectee:
    def write(self, _data: bytes):
        raise BrokenPipeError


class TestsInitSegmentMse(unittest.TestCase):
    def test_moov_tenant_dans_un_seul_bloc(self):
        """Cas simple, déjà correct avant le correctif : un seul appel suffit."""
        segment = _segment_synthetique()
        lecteur = serve.LecteurTube(FauxPipeMorceaux([segment]))
        resultat = serve.read_mp4_init_segment(lecteur, seconds=2.0)
        self.assertEqual(resultat, segment)
        self.assertEqual(serve.h264_mime_codec_from_moov(resultat), "avc1.640028")

    def test_moov_etale_sur_deux_blocs_est_reassemble(self):
        """Bug corrigé le 18 août 2026 (28.22) : lancer un fil par appel sur
        le même tube laissait un fil orphelin bloqué dès qu'un appel
        n'aboutissait pas dans son délai ; un second appel concurrent sur le
        même tube pouvait perdre les octets suivants à son profit. Reproduit
        en vrai sur une caméra plus lente à répondre (moov étalé sur
        plusieurs blocs), jamais sur une caméra qui répond vite."""
        segment = _segment_synthetique()
        coupure = 100  # tombe au milieu de trak : le premier bloc seul est incomplet
        lecteur = serve.LecteurTube(
            FauxPipeMorceaux([segment[:coupure], segment[coupure:]])
        )

        resultat = serve.read_mp4_init_segment(lecteur, seconds=2.0)

        self.assertEqual(resultat, segment)
        self.assertEqual(serve.h264_mime_codec_from_moov(resultat), "avc1.640028")

    def test_moov_incomplet_a_la_fin_du_flux_renvoie_ce_qui_est_arrive(self):
        """Le tube se ferme (EOF) avant un avcC complet : pas d'exception,
        juste ce qui a pu être accumulé — c'est à l'appelant de traiter un
        résultat vide ou incomplet comme un échec de direct."""
        segment = _segment_synthetique()
        lecteur = serve.LecteurTube(FauxPipeMorceaux([segment[:100], b""]))
        resultat = serve.read_mp4_init_segment(lecteur, seconds=2.0)
        self.assertEqual(resultat, segment[:100])


class TestsLecteurTube(unittest.TestCase):
    def test_lire_rend_none_sur_delai_sans_perdre_la_donnee_qui_suit(self):
        """Bug corrigé le 18 août 2026 (28.26) : la boucle d'envoi principale
        de /live et /live-mse lisait le tube en direct
        (process.stdout.read(16384)), sans aucun délai réel — seule la
        condition ENTRE deux lectures regardait LIVE_MAX_SECONDS, ce qui ne
        bornait rien si une lecture individuelle bloquait plus longtemps
        (vu en vrai : un direct resté ouvert plus de 600 s, MODULE_SLOT
        jamais rendu, alors que LIVE_MAX_SECONDS vaut 300). ``lire(délai)``
        doit rendre ``None`` (pas ``b""``, qui signifierait EOF) dès que le
        délai est écoulé, sans jamais perdre la donnée qui arrive ensuite."""
        pipe = FauxPipeLent(attente=0.3, morceau=b"donnees")
        lecteur = serve.LecteurTube(pipe)

        premier = lecteur.lire(0.05)
        self.assertIsNone(premier)

        second = lecteur.lire(1.0)
        self.assertEqual(second, b"donnees")

    def test_lire_rend_bytes_vides_sur_vraie_fin_de_tube(self):
        """EOF réel (tube fermé) distinct d'un simple délai écoulé : ``b""``,
        jamais ``None``, sans quoi l'appelant boucle indéfiniment sur un
        flux qui ne reviendra plus."""
        lecteur = serve.LecteurTube(FauxPipeMorceaux([b""]))
        self.assertEqual(lecteur.lire(1.0), b"")


class TestsCycleDirectMse(unittest.TestCase):
    def setUp(self):
        serve.MODULE_SLOT_INFO.clear()
        serve._effacer_erreur_direct()

    def tearDown(self):
        serve.MODULE_SLOT_INFO.clear()
        serve._effacer_erreur_direct()

    @staticmethod
    def handler(wfile=None):
        handler = serve.Handler.__new__(serve.Handler)
        handler.ffmpeg = "ffmpeg-test"
        handler.wfile = wfile if wfile is not None else io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.send_error = mock.Mock()
        return handler

    @staticmethod
    def slot_est_libre(slot) -> bool:
        libre = slot.acquire(blocking=False)
        if libre:
            slot.release()
        return libre

    def test_503_n_est_envoye_qu_apres_nettoyage_et_remplace_erreur_obsolete(self):
        slot = threading.Semaphore(1)
        verrou = FauxVerrou()
        handler = self.handler()
        etat_lors_du_503 = {}
        erreur_vue_par_blink = []
        serve._memoriser_erreur_direct("Jardin", "ancienne erreur", 503)

        def echec_blink(*_args, **_kwargs):
            erreur_vue_par_blink.append(serve._derniere_erreur_direct())
            raise RuntimeError("Blink refuse le direct")

        def noter_503(code, message):
            etat_lors_du_503.update({
                "code": code,
                "message": message,
                "verrou_sorti": verrou.sorti,
                "slot_libre": self.slot_est_libre(slot),
                "slot_info": dict(serve.MODULE_SLOT_INFO),
            })

        handler.send_error = noter_503
        with mock.patch.object(serve, "MODULE_SLOT", slot), \
             mock.patch.object(serve.blink_engine, "hub_lock", return_value=verrou), \
             mock.patch.object(serve.BLINK, "call", side_effect=echec_blink):
            serve.Handler.send_live_mse(handler, "Jardin")

        self.assertEqual(erreur_vue_par_blink, [{}])
        self.assertEqual(etat_lors_du_503["code"], 503)
        self.assertEqual(etat_lors_du_503["message"], "Live stream unavailable")
        self.assertTrue(etat_lors_du_503["verrou_sorti"])
        self.assertTrue(etat_lors_du_503["slot_libre"])
        self.assertEqual(etat_lors_du_503["slot_info"], {})
        self.assertEqual(serve._derniere_erreur_direct(), {
            "camera": "Jardin", "message": "Blink refuse le direct", "status": 503,
        })

    def test_flux_cree_est_referme_si_son_demarrage_echoue(self):
        slot = threading.Semaphore(1)
        verrou = FauxVerrou()
        handler = self.handler()

        class FauxFlux:
            def __init__(self):
                self.arrete = False

            async def start(self):
                raise RuntimeError("demarrage refuse")

            def stop(self):
                self.arrete = True

        flux = FauxFlux()

        class FausseCamera:
            async def init_livestream(self):
                return flux

        def appel(coroutine_factory, timeout):
            return asyncio.run(coroutine_factory(object()))

        with mock.patch.object(serve, "MODULE_SLOT", slot), \
             mock.patch.object(serve.blink_engine, "hub_lock", return_value=verrou), \
             mock.patch.object(
                 serve.BLINK, "find_camera", return_value=(object(), FausseCamera())
             ), \
             mock.patch.object(serve.BLINK, "call", side_effect=appel):
            serve.Handler.send_live_mse(handler, "Jardin")

        self.assertTrue(flux.arrete)
        self.assertTrue(verrou.sorti)
        self.assertTrue(self.slot_est_libre(slot))
        handler.send_error.assert_called_once_with(503, "Live stream unavailable")

    def test_stderr_bytes_ne_bloque_plus_la_liberation_apres_deconnexion(self):
        slot = threading.Semaphore(1)
        verrou = FauxVerrou()
        process = FauxProcessus(
            _segment_synthetique(),
            b"dimensions not set\nCould not write header",
        )
        handler = self.handler(SortieDeconnectee())

        with mock.patch.object(serve, "MODULE_SLOT", slot), \
             mock.patch.object(serve.blink_engine, "hub_lock", return_value=verrou), \
             mock.patch.object(serve.BLINK, "call", return_value="rtsp://camera"), \
             mock.patch.object(serve.runtime, "demarrer", return_value=process), \
             mock.patch("builtins.print") as journal:
            serve.Handler.send_live_mse(handler, "Salon")

        self.assertTrue(process.termine)
        self.assertTrue(process.attendu)
        self.assertTrue(verrou.sorti)
        self.assertTrue(self.slot_est_libre(slot))
        self.assertEqual(serve.MODULE_SLOT_INFO, {})
        self.assertIn(
            "dimensions not set",
            " ".join(str(call) for call in journal.call_args_list),
        )

    def test_echec_secondaire_du_verrou_ne_fait_pas_fuir_le_slot(self):
        slot = threading.Semaphore(1)
        verrou = FauxVerrou(echec_sortie=True)
        handler = self.handler()

        with mock.patch.object(serve, "MODULE_SLOT", slot), \
             mock.patch.object(serve.blink_engine, "hub_lock", return_value=verrou), \
             mock.patch.object(
                 serve.BLINK, "call", side_effect=RuntimeError("camera indisponible")
             ):
            serve.Handler.send_live_mse(handler, "Entrée")

        self.assertTrue(verrou.sorti)
        self.assertTrue(self.slot_est_libre(slot))
        self.assertEqual(serve.MODULE_SLOT_INFO, {})
        handler.send_error.assert_called_once()
        self.assertEqual(handler.send_error.call_args.args[0], 503)

    def test_409_publie_le_refus_courant_pas_un_ancien_503(self):
        slot = threading.Semaphore(1)
        slot.acquire()
        serve.MODULE_SLOT_INFO.update({
            "quoi": "direct MSE", "camera": "Salon", "depuis": time.monotonic(),
        })
        serve._memoriser_erreur_direct("Jardin", "ancienne erreur", 503)
        handler = self.handler()
        try:
            with mock.patch.object(serve, "MODULE_SLOT", slot):
                serve.Handler.send_live_mse(handler, "Jardin")
        finally:
            slot.release()

        handler.send_error.assert_called_once()
        code, raison = handler.send_error.call_args.args
        self.assertEqual(code, 409)
        self.assertEqual(raison, "Live stream busy")
        message = serve._derniere_erreur_direct()["message"]
        self.assertIn("Salon", message)
        self.assertEqual(serve._derniere_erreur_direct(), {
            "camera": "Jardin", "message": message, "status": 409,
        })


if __name__ == "__main__":
    unittest.main()
