"""Issue #155 : plus aucun enregistrement récent détecté dans le cloud.

Depuis le 23/11/2026, ``/api/v1/accounts/<compte>/media/changed`` répond
toujours HTTP 200 mais ne liste plus les clips récents (issues GitHub blinkpy
#1354 et blink2video #155) : l'application Blink les voit, blink2video non.
L'inventaire v4 les rend, du plus récent au plus ancien, paginé par
``pagination_key``, avec un identifiant 64 bits et une URL de média absolue.

Ces tests n'ouvrent aucune socket : ``Auth.query`` est remplacé par un double.
Exécution :

    python -B -m unittest -v test_blink_models_manifeste_cloud_v4.py
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import unittest
from types import SimpleNamespace

import blink_models


def instant(jours_avant: float = 1) -> str:
    """Horodatage ISO UTC, relatif à l'instant du test."""
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(days=jours_avant)).isoformat()


def entree(identifiant=1, *, jours_avant: float = 1, camera="jardin", **extra):
    """Entrée d'inventaire v4, telle que Blink la sert aujourd'hui."""
    donnees = {
        "id": identifiant,
        "device_name": camera,
        "created_at": instant(jours_avant),
        "media": (
            "https://rest-test.invalid/api/v4/accounts/42/media/"
            f"EVM-{identifiant}/video/contents"
        ),
        "network_id": 7,
        "device_id": "cam-jardin",
        "type": "video",
        "deleted": False,
        "partial": False,
    }
    donnees.update(extra)
    return donnees


class FauxBlinkV4:
    """La surface que ``read_cloud_manifest`` emprunte à blinkpy.

    ``pages`` est servi dans l'ordre, une réponse par appel ; une fois épuisé,
    l'appel rend ``None`` comme le vrai ``Auth.query`` après une panne."""

    def __init__(self, pages=(), inventaire_v1=None):
        self.pages = list(pages)
        self.inventaire_v1 = [] if inventaire_v1 is None else inventaire_v1
        self.requetes = []
        self.appels_v1 = []
        self.urls = SimpleNamespace(base_url="https://rest-test.invalid")
        self.account_id = "42"
        self.auth = SimpleNamespace(
            header={"Authorization": "Bearer t"}, query=self._query)

    async def _query(self, **requete):
        self.requetes.append(requete)
        return self.pages.pop(0) if self.pages else None

    async def get_videos_metadata(self, **options):
        self.appels_v1.append(options)
        return self.inventaire_v1


class InventaireV4(unittest.IsolatedAsyncioTestCase):
    async def _lire(self, blink, since_days=None):
        with contextlib.redirect_stdout(io.StringIO()) as sortie:
            clips = await blink_models.read_cloud_manifest(blink, since_days)
        return clips, sortie.getvalue()

    async def test_l_inventaire_v4_est_la_source(self):
        """v1 ne liste plus rien de récent : v4 doit être interrogé seul."""
        blink = FauxBlinkV4([{"media": [entree(11)], "pagination_key": 0}])
        clips, _ = await self._lire(blink)
        self.assertEqual([clip.id for clip in clips], [11])
        self.assertEqual(blink.appels_v1, [])
        self.assertEqual(
            blink.requetes[0]["url"],
            "https://rest-test.invalid/api/v4/accounts/42/media",
        )

    async def test_suit_la_cle_de_pagination(self):
        blink = FauxBlinkV4([
            {"media": [entree(1)], "pagination_key": 111},
            {"media": [entree(2)], "pagination_key": 222},
            {"media": []},
        ])
        clips, _ = await self._lire(blink)
        self.assertEqual([clip.id for clip in clips], [1, 2])
        self.assertNotIn("pagination_key", blink.requetes[0]["url"])
        self.assertTrue(blink.requetes[1]["url"].endswith("?pagination_key=111"))
        self.assertTrue(blink.requetes[2]["url"].endswith("?pagination_key=222"))

    async def test_une_cle_repete_s_arrete(self):
        blink = FauxBlinkV4([
            {"media": [entree(1)], "pagination_key": 5},
            {"media": [entree(2)], "pagination_key": 5},
        ])
        clips, _ = await self._lire(blink)
        self.assertEqual([clip.id for clip in clips], [1, 2])
        self.assertEqual(len(blink.requetes), 2)

    async def test_page_entierement_hors_fenetre_arrete_la_lecture(self):
        blink = FauxBlinkV4([{
            "media": [entree(1, jours_avant=45), entree(2, jours_avant=40)],
            "pagination_key": 9,
        }])
        clips, _ = await self._lire(blink)
        self.assertEqual(clips, [])
        self.assertEqual(len(blink.requetes), 1)

    async def test_page_melangee_garde_les_clips_de_la_fenetre(self):
        blink = FauxBlinkV4([{
            "media": [entree(1, jours_avant=45), entree(2, jours_avant=1)],
            "pagination_key": 9,
        }])
        clips, _ = await self._lire(blink)
        self.assertEqual([clip.id for clip in clips], [2])

    async def test_filtre_supprimes_partiels_et_non_videos(self):
        blink = FauxBlinkV4([{"media": [
            entree(1),
            entree(2, deleted=True),
            entree(3, partial=True),
            entree(4, type="image"),
            entree(5),
        ]}])
        clips, _ = await self._lire(blink)
        self.assertEqual([clip.id for clip in clips], [1, 5])

    async def test_entree_sans_date_est_signalee(self):
        sans_date = entree(2)
        sans_date.pop("created_at")
        blink = FauxBlinkV4([{"media": [entree(1), sans_date]}])
        clips, sortie = await self._lire(blink)
        self.assertEqual([clip.id for clip in clips], [1])
        self.assertIn(
            blink_models.msg("cloud_entrees_invalides", invalides=1), sortie)

    async def test_plafond_de_pagination_signale(self):
        pages = [
            {"media": [entree(numero)], "pagination_key": numero}
            for numero in range(1, blink_models.PLAFOND_PAGES_CLOUD_V4 + 1)
        ]
        blink = FauxBlinkV4(pages)
        clips, sortie = await self._lire(blink)
        self.assertEqual(len(clips), blink_models.PLAFOND_PAGES_CLOUD_V4)
        self.assertEqual(len(blink.requetes), blink_models.PLAFOND_PAGES_CLOUD_V4)
        self.assertIn(
            blink_models.msg("cloud_v4_plafond",
                             pages=blink_models.PLAFOND_PAGES_CLOUD_V4),
            sortie,
        )

    async def test_reponse_vide_ne_bascule_pas_sur_v1(self):
        """Une page valide sans média est une réponse : rien de neuf."""
        blink = FauxBlinkV4([{"media": []}], inventaire_v1=[entree(7)])
        clips, _ = await self._lire(blink)
        self.assertEqual(clips, [])
        self.assertEqual(blink.appels_v1, [])

    async def test_page_perdue_apres_une_page_garde_le_lu(self):
        blink = FauxBlinkV4([
            {"media": [entree(1)], "pagination_key": 9},
            None,
        ])
        clips, _ = await self._lire(blink)
        self.assertEqual([clip.id for clip in clips], [1])
        self.assertEqual(blink.appels_v1, [])

    async def test_identifiant_64_bits_conserve(self):
        identifiant = 2 ** 53 + 1
        blink = FauxBlinkV4([{"media": [entree(identifiant)]}])
        clips, _ = await self._lire(blink)
        self.assertEqual(clips[0].id, identifiant)
        self.assertEqual(clips[0].network_id, 7)


class RepliV1(unittest.IsolatedAsyncioTestCase):
    async def _lire(self, blink, since_days=None):
        with contextlib.redirect_stdout(io.StringIO()) as sortie:
            clips = await blink_models.read_cloud_manifest(blink, since_days)
        return clips, sortie.getvalue()

    async def test_sans_reponse_v4_replie_sur_l_ancien_manifeste(self):
        blink = FauxBlinkV4([None], inventaire_v1=[entree(7)])
        clips, sortie = await self._lire(blink)
        self.assertEqual([clip.id for clip in clips], [7])
        self.assertEqual(len(blink.appels_v1), 1)
        self.assertIn(blink_models.msg("cloud_v4_indisponible"), sortie)

    async def test_reponse_sans_liste_de_medias_replie(self):
        """Un corps d'erreur JSON n'est pas un inventaire vide."""
        blink = FauxBlinkV4(
            [{"code": 700, "message": "Media not found"}],
            inventaire_v1=[entree(7)],
        )
        clips, sortie = await self._lire(blink)
        self.assertEqual([clip.id for clip in clips], [7])
        self.assertEqual(len(blink.appels_v1), 1)
        self.assertIn(blink_models.msg("cloud_v4_indisponible"), sortie)

    async def test_repli_utilise_la_fenetre_demandee(self):
        blink = FauxBlinkV4([None])
        await self._lire(blink, 7)
        options = blink.appels_v1[0]
        self.assertEqual(options["stop"], blink_models.PLAFOND_PAGES_CLOUD)
        since = dt.datetime.strptime(options["since"], "%Y/%m/%d %H:%M:%S")
        ecart = (dt.datetime.now() - since).total_seconds()
        self.assertAlmostEqual(ecart, 7 * 86400, delta=120)

    async def test_fenetre_negative_leve_avant_toute_requete(self):
        blink = FauxBlinkV4([{"media": [entree(1)]}])
        with self.assertRaises(ValueError):
            await blink_models.read_cloud_manifest(blink, -1)
        self.assertEqual(blink.requetes, [])
        self.assertEqual(blink.appels_v1, [])


class AdresseMedia(unittest.IsolatedAsyncioTestCase):
    async def test_une_url_absolue_n_est_pas_prefixee(self):
        """v4 donne une URL orientée vers le centre de données du compte."""
        blink = FauxBlinkV4()
        adresse = (
            "https://rest-test.invalid/api/v4/accounts/42/media/"
            "EVM-1/video/contents"
        )
        await blink_models.demander_media_cloud(blink, adresse)
        self.assertEqual(blink.requetes[0]["url"], adresse)

    async def test_un_chemin_relatif_reste_prefixe(self):
        """L'ancien manifeste, lui, ne donnait qu'un chemin relatif."""
        blink = FauxBlinkV4()
        await blink_models.demander_media_cloud(blink, "/api/v2/media/3.mp4")
        self.assertEqual(
            blink.requetes[0]["url"],
            "https://rest-test.invalid/api/v2/media/3.mp4",
        )


if __name__ == "__main__":
    unittest.main()
