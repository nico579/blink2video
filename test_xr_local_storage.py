"""Découverte du stockage local sans dépendre des anciens endpoints blinkpy."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest import mock


os.environ["BLINK_BOOTSTRAP"] = "none"

import blink_models  # noqa: E402
import blink_auth  # noqa: E402
import blink_cli  # noqa: E402
import blink_engine  # noqa: E402


class FauxAppareilSansModule:
    """Surface d'un BlinkLotus/Owl : son ID est celui de la caméra."""

    def __init__(self, identifiant, reseau):
        self.sync_id = identifiant
        self.network_id = reseau


class FauxSessionHTTP:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def compte_avec_stockage_local():
    appareil = FauxAppareilSansModule(123, "7")
    blink = SimpleNamespace(
        account_id=42,
        auth=SimpleNamespace(region_id="u006"),
        motion_interval=5,
        urls=SimpleNamespace(base_url="https://rest-u006.immedia-semi.com"),
        sync={"Porte d'entrée": appareil},
        cameras={},
        homescreen={
            "networks": [{"id": 7, "name": "Jardin", "armed": True}],
            "sync_modules": [{
                "id": 900,
                "network_id": 7,
                "name": "My Blink Sync Module",
                "serial": "ANONYMIZED",
                "status": "online",
                "fw_version": "1.2.3",
                "local_storage_enabled": True,
                "local_storage_compatible": True,
                "local_storage_status": "active",
            }],
            "cameras": [],
            "owls": [],
            "doorbells": [{
                "id": 123,
                "network_id": 7,
                "name": "Porte d'entrée",
                "onboarded": True,
            }],
        },
    )
    return blink, appareil


class DecouverteStockageLocalTests(unittest.IsolatedAsyncioTestCase):
    async def test_homescreen_fournit_le_vrai_module_pas_l_id_de_la_doorbell(self):
        blink, appareil = compte_avec_stockage_local()

        modules = blink_models.select_sync_modules(blink, None)

        self.assertEqual([nom for nom, _sync in modules], ["Jardin"])
        sync = modules[0][1]
        self.assertIsNot(sync, appareil)
        self.assertEqual(str(sync.sync_id), "900")
        self.assertEqual(str(sync.network_id), "7")
        self.assertTrue(sync._local_storage["compatible"])
        self.assertTrue(sync.local_storage)

    async def test_poll_du_manifeste_recoit_les_ids_du_module(self):
        from blinkpy import api

        blink, _appareil = compte_avec_stockage_local()
        sync = blink_models.select_sync_modules(blink, None)[0][1]
        demander = mock.AsyncMock(return_value={"id": 51})
        recevoir = mock.AsyncMock(return_value={"manifest_id": 61, "clips": []})

        with mock.patch.object(api, "request_local_storage_manifest", new=demander), \
             mock.patch.object(api, "get_local_storage_manifest", new=recevoir):
            requete = await sync.poll_local_storage_manifest()
            manifeste = await sync.poll_local_storage_manifest(requete["id"])

        self.assertEqual(manifeste["manifest_id"], 61)
        demander.assert_awaited_once_with(blink, "7", 900)
        recevoir.assert_awaited_once_with(blink, "7", 900, 51)

    async def test_un_id_identique_sur_un_autre_reseau_n_est_pas_reutilise(self):
        blink, _appareil = compte_avec_stockage_local()
        autre = FauxAppareilSansModule(900, "8")
        blink.sync = {"Autre réseau": autre}

        sync = blink_models.select_sync_modules(blink, None)[0][1]

        self.assertIsNot(sync, autre)
        self.assertEqual(autre.network_id, "8")
        self.assertEqual(sync.network_id, "7")

    async def test_stockage_actif_etablit_la_compatibilite_si_champ_omis(self):
        blink, _appareil = compte_avec_stockage_local()
        del blink.homescreen["sync_modules"][0]["local_storage_compatible"]

        sync = blink_models.select_sync_modules(blink, None)[0][1]

        self.assertTrue(sync._local_storage["compatible"])

    async def test_manifeste_utilise_id_module_et_restitue_nom_camera(self):
        blink, _appareil = compte_avec_stockage_local()
        sync = blink_models.select_sync_modules(blink, "Jardin")[0][1]
        reponses = [
            {"id": 51},
            {
                "manifest_id": 61,
                "clips": [{
                    "id": 71,
                    "camera_name": "Portedentrée",
                    "created_at": "2026-08-30T10:00:00+00:00",
                    "size": "128",
                }],
            },
        ]

        with mock.patch.object(
            sync, "poll_local_storage_manifest", new=mock.AsyncMock(side_effect=reponses),
        ):
            clips = await blink_models.read_local_manifest(sync)

        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].name, "Porte d'entrée")
        self.assertIn("/networks/7/sync_modules/900/", clips[0].url())

    async def test_sans_homescreen_exploitable_conserve_le_comportement_blinkpy(self):
        existant = FauxAppareilSansModule(12, "3")
        blink = SimpleNamespace(sync={"Existant": existant}, homescreen={})

        self.assertEqual(
            blink_models.select_sync_modules(blink, None),
            [("Existant", existant)],
        )

    async def test_cli_usb_accepte_un_module_homescreen_quand_blink_sync_est_vide(self):
        blink, _appareil = compte_avec_stockage_local()
        blink.sync = {}
        arguments = SimpleNamespace(command="list", source="usb", hub=None)
        boucle = mock.AsyncMock(return_value=0)

        with mock.patch.object(
            blink_auth, "session_http_temporaire", return_value=FauxSessionHTTP(),
        ), mock.patch.object(
            blink_auth, "connect", new=mock.AsyncMock(return_value=blink),
        ), mock.patch.object(
            blink_engine, "boucler", new=boucle,
        ), mock.patch("builtins.print"):
            code = await blink_cli.main(arguments)

        self.assertEqual(code, 0)
        modules = boucle.await_args.args[2]
        self.assertEqual([(nom, sync.sync_id) for nom, sync in modules], [("Jardin", 900)])


if __name__ == "__main__":
    unittest.main()
