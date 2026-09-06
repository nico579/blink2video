"""Bug rapporté le 2026-09-06 : la fin normale du flux WebRTC jetait les
images encore en attente (_terminer_file() vidait la file au lieu de la
laisser se lire) et perdait la toute dernière image reçue, jamais close par
un nouvel AUD ni extraite du tampon du démultiplexeur - ni à l'écran, ni à
l'enregistrement. Test original de Nico : sur une source de 60 images, 59
disparaissaient de la file et seules 59 atteignaient l'enregistrement."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import blink_webrtc


def _boite(genre: int, charge: bytes) -> bytes:
    return b"\x00\x00\x00\x01" + bytes([genre]) + charge


AUD = _boite(9, b"\x10")


def image(indice: int) -> bytes:
    return _boite(5, f"image-{indice}".encode())


@unittest.skipUnless(blink_webrtc.DISPONIBLE, "aiortc optionnel non installé")
class TestsFinNormaleFluxWebRTC(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="blink-webrtc-fin-")
        self.reader = asyncio.StreamReader()
        self.track = blink_webrtc._PisteH264(
            self.reader, ffmpeg="ffmpeg-factice",
            chemin_enregistrement=lambda: Path(self.tmp.name) / "direct.mp4",
            enregistrement_actif=lambda: False,
        )
        self.enregistrements = []
        self.track._synchroniser_enregistrement = self.enregistrements.append
        self.track.sps_pps_pret.set()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def _vider_file(self) -> list:
        images = []
        while not self.track._file.empty():
            item = self.track._file.get_nowait()
            if item is not None:
                images.append(item)
        return images

    async def test_fin_normale_transmet_toutes_les_images_y_compris_la_derniere(self):
        # 60 images : les 59 premières bien closes par l'AUD suivante, la
        # 60e jamais close - exactement la forme d'un vrai flux qui s'arrête
        # en plein milieu d'une image (la caméra ferme le flux TS, elle
        # n'attend jamais qu'un 61e AUD la confirme).
        nals_alimenter = [AUD]
        for i in range(60):
            nals_alimenter.append(image(i))
            if i < 59:
                nals_alimenter.append(AUD)
        # finaliser() renvoie ce que le démultiplexeur aurait gardé en
        # tampon : ici, rien de plus (l'image 60 est déjà entière dans
        # alimenter() pour ce test - le point testé est le flush de
        # _unite_courante, pas celui du démultiplexeur, couvert séparément
        # par test_blink_ts_demux via un appel direct sur DemuxeurTSVideo).
        with mock.patch.object(
            blink_webrtc.blink_ts_demux.DemuxeurTSVideo, "alimenter",
            return_value=[(i, nal) for i, nal in enumerate(nals_alimenter)],
        ):
            self.reader.feed_data(b"un seul bloc TS, peu importe son contenu reel")
            self.reader.feed_eof()
            await asyncio.wait_for(self.track._tache, 1)

        # Chaque unité flushée porte son AUD de tête (il précède l'image
        # qu'il délimite, il n'est jeté nulle part).
        attendu = [AUD + image(i) for i in range(60)]
        images = self._vider_file()
        self.assertEqual(len(images), 60, "les 60 images doivent atteindre la file")
        self.assertEqual([nal for _, nal in images], attendu)
        self.assertEqual(len(self.enregistrements), 60,
                          "les 60 images doivent atteindre l'enregistrement")
        self.assertEqual(self.enregistrements, attendu)

    async def test_demultiplexeur_finalise_est_integre_a_la_derniere_image(self):
        """La toute dernière NAL peut aussi être coupée AU NIVEAU du
        démultiplexeur (pas encore vue par alimenter()) : finaliser() doit
        être appelée et son résultat traité comme le reste."""
        with mock.patch.object(
            blink_webrtc.blink_ts_demux.DemuxeurTSVideo, "alimenter",
            return_value=[(0, AUD), (0, image(0))],
        ), mock.patch.object(
            blink_webrtc.blink_ts_demux.DemuxeurTSVideo, "finaliser",
            return_value=[(1, image(1))],
        ):
            self.reader.feed_data(b"bloc TS")
            self.reader.feed_eof()
            await asyncio.wait_for(self.track._tache, 1)

        images = self._vider_file()
        self.assertEqual([nal for _, nal in images], [AUD + image(0) + image(1)])

    async def test_arret_force_continue_de_tout_vider_immediatement(self):
        """Non-régression : fermer() (arrêt explicite, pas fin de flux) doit
        garder le comportement d'avant - jeter la file tout de suite, ne pas
        attendre qu'un navigateur qui ne reviendra peut-être jamais la lise."""
        with mock.patch.object(
            blink_webrtc.blink_ts_demux.DemuxeurTSVideo, "alimenter",
            return_value=[(0, AUD), (0, image(0)), (1, AUD), (1, image(1))],
        ):
            self.reader.feed_data(b"bloc TS")
            # Ne pas feed_eof() : le flux "traîne", comme si le navigateur
            # avait fermé la connexion WebRTC en premier.
            await asyncio.sleep(0)
            self.track.fermer()
            # fermer() annule _tache : l'attendre directement propagerait
            # CancelledError à ce test (cf. TestsEnregistrementWebRTC plus
            # haut, même pattern avec return_exceptions=True).
            await asyncio.wait_for(
                asyncio.gather(self.track._tache, return_exceptions=True), 1
            )

        self.assertTrue(self.track._file.get_nowait() is None,
                         "fermer() doit poser directement le sentinel, file vidée")
        # image(1) n'a jamais été flushée (aucun AUD suivant, et ce n'est
        # pas une fin normale) : comportement inchangé, pas de perte
        # nouvelle introduite pour ce chemin.
        self.assertEqual(self.enregistrements, [AUD + image(0)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
