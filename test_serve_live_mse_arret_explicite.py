"""Bug rapporté le 2026-09-06 : arrêter un direct MSE silencieux (aucun
bloc ne vient plus de la caméra) pouvait laisser le module occupé jusqu'à
LIVE_MAX_SECONDS (300 s) - la fermeture du navigateur n'était détectée qu'à
la prochaine écriture, qui pouvait ne jamais survenir."""

from __future__ import annotations

import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-live-mse-arret-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import serve  # noqa: E402 - bootstrap neutralisé avant import


def _boite(genre: bytes, charge: bytes) -> bytes:
    return (8 + len(charge)).to_bytes(4, "big") + genre + charge


def _segment_synthetique() -> bytes:
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


class FauxPipeSilencieuxApresInit:
    """Rend le segment d'initialisation une fois, puis bloque indéfiniment :
    une caméra dont le flux reste ouvert mais ne produit plus rien, jamais
    d'EOF - le cas qui, avant ce correctif, ne pouvait être détecté qu'au
    bout de LIVE_MAX_SECONDS."""

    def __init__(self, segment_initial: bytes):
        self._segment_initial = segment_initial
        self._premier_rendu = False

    def read(self, _n: int) -> bytes:
        if not self._premier_rendu:
            self._premier_rendu = True
            return self._segment_initial
        time.sleep(60)  # jamais atteint : le test se termine bien avant
        return b""


class FauxVerrou:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FauxProcessus:
    def __init__(self, pipe):
        self.stdout = pipe
        self.stderr = io.BytesIO(b"")

    def terminate(self):
        pass

    def wait(self, timeout=None):
        pass

    def kill(self):
        pass


class TestsArretExpliciteDirectSilencieux(unittest.TestCase):
    def setUp(self):
        serve.MODULE_SLOT_INFO.clear()
        serve._effacer_erreur_direct()
        serve.DIRECT_MSE_SESSION.clear()
        serve.DIRECT_ARRETS_RECENTS.clear()
        self.addCleanup(serve.MODULE_SLOT_INFO.clear)
        self.addCleanup(serve._effacer_erreur_direct)
        self.addCleanup(serve.DIRECT_MSE_SESSION.clear)
        self.addCleanup(serve.DIRECT_ARRETS_RECENTS.clear)
        self._dossier_direct = tempfile.TemporaryDirectory(prefix="blink-direct-")
        self.addCleanup(self._dossier_direct.cleanup)
        patch_dossier = mock.patch.object(
            serve, "DOSSIER_DIRECT", Path(self._dossier_direct.name)
        )
        patch_dossier.start()
        self.addCleanup(patch_dossier.stop)

    @staticmethod
    def _handler():
        handler = serve.Handler.__new__(serve.Handler)
        handler.ffmpeg = "ffmpeg-test"
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.send_error = mock.Mock()
        return handler

    def test_arret_explicite_interrompt_l_attente_sans_attendre_live_max_seconds(self):
        session_id = "a" * 32
        segment = _segment_synthetique()
        pipe = FauxPipeSilencieuxApresInit(segment)
        process = FauxProcessus(pipe)
        handler = self._handler()
        termine = threading.Event()

        def lancer():
            with mock.patch.object(serve, "MODULE_SLOT", threading.Semaphore(1)), \
                 mock.patch.object(serve.blink_engine, "hub_lock", return_value=FauxVerrou()), \
                 mock.patch.object(serve.BLINK, "call", return_value="rtsp://camera"), \
                 mock.patch.object(serve.runtime, "demarrer", return_value=process):
                serve.Handler.send_live_mse(handler, "Jardin", session_id)
            termine.set()

        fil = threading.Thread(target=lancer, daemon=True)
        fil.start()
        try:
            # Attend que la session MSE soit bien enregistrée (course avec
            # le thread ci-dessus, sans rapport avec le bug lui-même).
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with serve.DIRECT_WEBRTC_SESSION_LOCK:
                    holder = serve.DIRECT_MSE_SESSION.get("session")
                if holder is not None and holder["session_id"] == session_id:
                    break
                time.sleep(0.01)
            else:
                self.fail("la session MSE n'a jamais été enregistrée")

            demande = serve._demander_arret_direct(session_id)
            self.assertTrue(demande)

            # Le silence est total (FauxPipeSilencieuxApresInit ne rend plus
            # rien après le premier bloc) : sans l'arrêt explicite, seul
            # LIVE_MAX_SECONDS (300 s) aurait fini par sortir la boucle.
            # Quelques secondes de marge sur LIVE_MSE_ARRET_POLL_SECONDS (1 s)
            # suffisent très largement à distinguer les deux.
            self.assertTrue(
                termine.wait(timeout=5),
                "send_live_mse() ne s'est pas arrêté après la demande explicite "
                "(serait resté bloqué jusqu'à LIVE_MAX_SECONDS sans le correctif)",
            )
        finally:
            fil.join(timeout=1)

        with serve.DIRECT_WEBRTC_SESSION_LOCK:
            self.assertIsNone(serve.DIRECT_MSE_SESSION.get("session"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
