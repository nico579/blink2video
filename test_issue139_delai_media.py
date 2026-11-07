"""Issue #139 : des clips cloud échouaient à chaque tour en « [write] TimeoutError ».

Chez MarkusKress, 19 clips sur 38 revenaient à chaque tour. blinkpy 0.25.9
demande 90 s pour un média, mais ``api.http_get`` perd ce paramètre en route :
la requête part avec les 10 s des appels JSON, et aiohttp applique ce délai
total à la lecture du corps. Tout clip dont le transfert durait plus de 10 s
était coupé. ``TimeoutError`` héritant d'``OSError``, le journal l'étiquetait
en plus « écriture ».

Le premier test rejoue ce trajet tel quel : le vrai ``Auth.query`` de blinkpy,
une vraie session aiohttp et un petit serveur local qui envoie un clip plus
lentement que le délai total d'origine, mais sans jamais se taire longtemps."""

import asyncio
import contextlib
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import aiohttp
from aiohttp import web
from blinkpy.auth import Auth
from blinkpy.blinkpy import Blink

import blink_engine
import blink_models
import runtime
from test_blink2video_audit import MP4_STRUCTUREL, FauxBlinkHTTP


def _delai_json_par_defaut(secondes):
    """Ramène les 10 s de ``Auth.query`` à une durée de test.

    ``timeout=TIMEOUT`` est figé à la définition de la méthode : remplacer la
    constante du module n'y changerait rien, c'est la valeur par défaut qu'il
    faut remplacer."""
    noms = [nom for nom, parametre in inspect.signature(Auth.query).parameters.items()
            if parametre.default is not inspect.Parameter.empty]
    defauts = list(Auth.query.__defaults__)
    defauts[noms.index("timeout")] = secondes
    return mock.patch.object(Auth.query, "__defaults__", tuple(defauts))


def _clip(numero=1):
    return blink_models.CloudClip({
        "id": numero, "device_name": "Hof",
        "created_at": "2026-11-07T04:02:22+00:00",
        "media": f"/api/v2/media/{numero}.mp4", "network_id": 7,
    })


class ServeurLent:
    """Envoie MP4_STRUCTUREL en morceaux espacés, comme un clip lourd."""

    def __init__(self, morceaux: int, pause: float):
        self.morceaux = morceaux
        self.pause = pause

    async def servir(self, request):
        reponse = web.StreamResponse(headers={"Content-Type": "video/mp4"})
        reponse.content_length = len(MP4_STRUCTUREL)
        await reponse.prepare(request)
        taille = -(-len(MP4_STRUCTUREL) // self.morceaux)
        for debut in range(0, len(MP4_STRUCTUREL), taille):
            await reponse.write(MP4_STRUCTUREL[debut:debut + taille])
            await asyncio.sleep(self.pause)
        return reponse

    @contextlib.asynccontextmanager
    async def demarrer(self):
        app = web.Application()
        app.router.add_get("/api/v2/media/{nom}", self.servir)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            await runner.cleanup()


class TransfertLentAvecBlinkpy(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dossier = tempfile.TemporaryDirectory(prefix="blink-139-")
        self.partiel = Path(self.dossier.name) / "clip.mp4.part"

    async def asyncTearDown(self):
        self.dossier.cleanup()

    async def _telecharger(self, silence_tolere, delai_json):
        """Transfert de 1,2 s environ, jamais plus de 0,3 s sans octet."""
        serveur = ServeurLent(morceaux=4, pause=0.3)
        async with serveur.demarrer() as base, aiohttp.ClientSession() as session:
            auth = Auth({"token": "jeton"}, no_prompt=True, session=session)
            blink = SimpleNamespace(auth=auth, urls=SimpleNamespace(base_url=base))
            clip = _clip()
            with mock.patch.object(blink_models, "SILENCE_MEDIA_CLOUD", silence_tolere), \
                    _delai_json_par_defaut(delai_json):
                ok = await clip.download_to(blink, self.partiel)
            return ok, clip

    async def test_temoin_blinkpy_coupe_le_transfert_au_delai_json(self):
        # Le défaut d'origine, sur le code de blinkpy lui-même : do_http_get
        # annonce 90 s mais la lecture du corps tombe au délai JSON. Si ce
        # témoin casse, blinkpy a corrigé api.http_get et demander_media_cloud
        # peut redevenir un simple appel à do_http_get.
        serveur = ServeurLent(morceaux=4, pause=0.3)
        async with serveur.demarrer() as base, aiohttp.ClientSession() as session:
            auth = Auth({"token": "jeton"}, no_prompt=True, session=session)
            blink = SimpleNamespace(auth=auth, urls=SimpleNamespace(base_url=base))
            with _delai_json_par_defaut(0.5):
                reponse = await Blink.do_http_get(blink, "/api/v2/media/1.mp4")
                with self.assertRaises(asyncio.TimeoutError):
                    await reponse.read()

    async def test_un_transfert_plus_long_que_le_delai_json_aboutit(self):
        # Délai JSON ramené à 0,5 s (10 s en vrai), silence toléré 1 s (60 s) :
        # avant le correctif, ce clip tombait en TimeoutError à chaque tour.
        ok, clip = await self._telecharger(silence_tolere=1, delai_json=0.5)
        self.assertTrue(ok, clip.download_issue)
        self.assertEqual(self.partiel.read_bytes(), MP4_STRUCTUREL)

    async def test_une_connexion_figee_tombe_en_reseau_pas_en_ecriture(self):
        # Silence toléré plus court que les pauses du serveur : le transfert
        # doit échouer, et le motif doit dire réseau, jamais écriture.
        ok, clip = await self._telecharger(silence_tolere=0.1, delai_json=10)
        self.assertFalse(ok)
        categorie, detail = clip.download_issue
        self.assertEqual(categorie, blink_models.msg("cat_reseau"))
        self.assertIn("TimeoutError", detail)  # SocketTimeoutError chez aiohttp
        self.assertFalse(self.partiel.exists())


class DelaiTransmis(unittest.IsolatedAsyncioTestCase):
    async def test_la_requete_borne_le_silence_et_non_la_duree(self):
        class Reponse:
            status = 200
            headers = {}

            async def read(self):
                return MP4_STRUCTUREL

        blink = FauxBlinkHTTP(Reponse())
        with tempfile.TemporaryDirectory(prefix="blink-139-") as dossier:
            self.assertTrue(await _clip(3).download_to(blink, Path(dossier) / "c.part"))
        (requete,) = blink.requetes
        self.assertEqual(requete["url"], "https://rest-test.invalid/api/v2/media/3.mp4")
        self.assertFalse(requete["json_resp"])
        delai = requete["timeout"]
        self.assertIsNone(delai.total)
        self.assertEqual(delai.sock_read, blink_models.SILENCE_MEDIA_CLOUD)


class CategorieDesErreurs(unittest.IsolatedAsyncioTestCase):
    async def _motif(self, erreur):
        class Flux:
            async def iter_chunked(self, _taille):
                yield MP4_STRUCTUREL[:10]
                raise erreur

        blink = FauxBlinkHTTP(SimpleNamespace(status=200, content=Flux(), headers={}))
        clip = _clip()
        with tempfile.TemporaryDirectory(prefix="blink-139-") as dossier:
            self.assertFalse(await clip.download_to(blink, Path(dossier) / "c.part"))
        return clip.download_issue[0]

    async def test_delai_et_connexion_sont_du_reseau(self):
        for erreur in (TimeoutError(), asyncio.TimeoutError(), ConnectionResetError(),
                       aiohttp.ClientPayloadError("coupé")):
            with self.subTest(erreur=type(erreur).__name__):
                self.assertEqual(await self._motif(erreur), blink_models.msg("cat_reseau"))

    async def test_disque_plein_reste_de_l_ecriture(self):
        self.assertEqual(await self._motif(OSError(28, "No space left on device")),
                         blink_models.msg("cat_ecriture"))


class ListeCloudVide(unittest.TestCase):
    """Une liste vide rendue par blinkpy après une requête ratée laissait un
    tour muet, comme un tour sans nouveauté. Seuls les changements s'écrivent."""

    def test_ecrit_le_passage_a_vide_puis_le_retour_seulement(self):
        with tempfile.TemporaryDirectory(prefix="blink-139-") as dossier, \
                mock.patch.object(runtime, "app_dir", return_value=Path(dossier)), \
                mock.patch.object(blink_engine, "_liste_cloud_vide", None), \
                mock.patch.object(runtime, "lire_langue", return_value="en"):
            for lus in (40, 40, 0, 0, 0, 38, 38):
                blink_engine._noter_liste_cloud(lus, 5411)
            journal = Path(dossier) / blink_engine.JOURNAL_TELECHARGEMENTS
            lignes = [ligne.split("  ", 1)[1] for ligne in
                      journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lignes, [
            "inventory  cloud: empty listing (registry 5411)",
            "inventory  cloud: listing back, 38 clip(s)",
        ])

    def test_un_premier_tour_vide_est_ecrit(self):
        with tempfile.TemporaryDirectory(prefix="blink-139-") as dossier, \
                mock.patch.object(runtime, "app_dir", return_value=Path(dossier)), \
                mock.patch.object(blink_engine, "_liste_cloud_vide", None):
            blink_engine._noter_liste_cloud(0, 0)
            blink_engine._noter_liste_cloud(0, 0)
            journal = Path(dossier) / blink_engine.JOURNAL_TELECHARGEMENTS
            self.assertEqual(len(journal.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
