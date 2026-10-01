"""Issue #55 : une caméra dont le nom a un accent ou un umlaut (« 1-Haustür »)
ne voyait jamais ses clips USB. Le Sync Module écrit son nom en ASCII
(« 1Haustr », relevé sur les fichiers d'une vraie clé), blinkpy le cherche dans
une table construite avec ``\\W+``, qui garde « ü » : pas de correspondance, et
le clip est jeté sans un mot. Le vrai code de blinkpy et de blink_models, avec
un manifeste simulé."""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"

import blink_models  # noqa: E402


def compte(*noms_cameras):
    """Un compte avec un Sync Module à stockage local actif et ces caméras."""
    return SimpleNamespace(
        account_id=42,
        auth=SimpleNamespace(region_id="u006"),
        motion_interval=5,
        urls=SimpleNamespace(base_url="https://rest-u006.immedia-semi.com"),
        sync={},
        cameras={},
        homescreen={
            "networks": [{"id": 7, "name": "Maison", "armed": True}],
            "sync_modules": [{
                "id": 900, "network_id": 7, "name": "My Blink Sync Module",
                "serial": "ANONYMIZED", "status": "online", "fw_version": "1.2.3",
                "local_storage_enabled": True, "local_storage_compatible": True,
                "local_storage_status": "active",
            }],
            "cameras": [
                {"id": 100 + i, "network_id": 7, "name": nom, "onboarded": True}
                for i, nom in enumerate(noms_cameras)
            ],
            "owls": [],
            "doorbells": [],
        },
    )


def manifeste(*noms_du_module):
    """Réponse du Sync Module : ses propres noms, tels que la clé les écrit."""
    return [
        {"id": 51},
        {
            "manifest_id": 61,
            "clips": [
                {"id": 70 + i, "camera_name": nom,
                 "created_at": "2026-10-01T05:17:37+00:00", "size": "128"}
                for i, nom in enumerate(noms_du_module)
            ],
        },
    ]


async def lire(noms_cameras, noms_du_module):
    sync = blink_models.select_sync_modules(compte(*noms_cameras), "Maison")[0][1]
    sortie = io.StringIO()
    with mock.patch.object(
        sync, "poll_local_storage_manifest",
        new=mock.AsyncMock(side_effect=manifeste(*noms_du_module)),
    ), contextlib.redirect_stdout(sortie):
        clips = await blink_models.read_local_manifest(sync)
    return clips, sortie.getvalue()


class FormesDuNom(unittest.TestCase):
    def test_haustuer_donne_les_trois_formes_plausibles(self):
        self.assertEqual(blink_models._formes_nom_manifeste("1-Haustür"),
                         {"1Haustr", "1Haustur", "1Haustuer"})

    def test_un_nom_deja_ascii_garde_sa_forme_d_origine_seule(self):
        self.assertEqual(blink_models._formes_nom_manifeste("Salon 2"), {"Salon2"})
        self.assertEqual(blink_models._formes_nom_manifeste("Cave_nord"), {"Cave_nord"})

    def test_un_nom_sans_aucun_caractere_ascii_n_a_aucune_forme(self):
        self.assertEqual(blink_models._formes_nom_manifeste("客厅"), set())


class ClipsDUneCameraAccentuee(unittest.IsolatedAsyncioTestCase):
    async def test_le_cas_du_rapport_umlaut_supprime_par_le_module(self):
        # Les deux caméras du rapport, un clip chacune.
        clips, _ = await lire(["1-Haustür", "2-Treppenhaus"],
                              ["1Haustr", "2Treppenhaus"])
        self.assertEqual(sorted(c.name for c in clips), ["1-Haustür", "2-Treppenhaus"])

    async def test_aussi_quand_le_module_translitere_ou_retire_l_accent(self):
        for ecrit in ("1Haustuer", "1Haustur"):
            with self.subTest(ecrit=ecrit):
                clips, _ = await lire(["1-Haustür"], [ecrit])
                self.assertEqual([c.name for c in clips], ["1-Haustür"])

    async def test_accents_francais(self):
        clips, _ = await lire(["Séjour", "Bébé"], ["Sjour", "Bb"])
        self.assertEqual(sorted(c.name for c in clips), ["Bébé", "Séjour"])

    async def test_une_camera_ascii_n_est_pas_modifiee(self):
        clips, sortie = await lire(["Salon"], ["Salon"])
        self.assertEqual([c.name for c in clips], ["Salon"])
        self.assertNotIn("ignoré", sortie)

    async def test_deux_cameras_au_nom_identique_sans_accent_ne_sont_jamais_confondues(self):
        # « Café » et « Cafè » donnent la même forme : le clip n'est attribué à
        # aucune des deux (son nom fait le dossier), il est signalé.
        clips, sortie = await lire(["Café", "Cafè"], ["Caf"])
        self.assertEqual(clips, [])
        self.assertIn("« Caf »", sortie)

    async def test_un_nom_vraiment_inconnu_est_signale_au_lieu_d_etre_jete_en_silence(self):
        clips, sortie = await lire(["Salon"], ["Salon", "Fantome", "Fantome"])
        self.assertEqual([c.name for c in clips], ["Salon"])
        self.assertIn("2 clip(s)", sortie)
        self.assertIn("« Fantome »", sortie)

    async def test_l_instance_retrouve_sa_methode_apres_la_lecture(self):
        sync = blink_models.select_sync_modules(compte("Salon"), "Maison")[0][1]
        self.assertNotIn("poll_local_storage_manifest", vars(sync))
        with mock.patch.object(
            sync, "poll_local_storage_manifest",
            new=mock.AsyncMock(side_effect=manifeste("Salon")),
        ) as simule, contextlib.redirect_stdout(io.StringIO()):
            await blink_models.read_local_manifest(sync)
            self.assertIs(sync.poll_local_storage_manifest, simule)
        self.assertNotIn("poll_local_storage_manifest", vars(sync))

    async def test_le_message_existe_dans_les_deux_langues(self):
        for langue in ("fr", "en"):
            with mock.patch.object(blink_models.runtime, "lire_langue",
                                   return_value=langue):
                texte = blink_models.msg("clips_nom_inconnu", nombre=3, noms="« X »")
            self.assertIn("3", texte)
            self.assertIn("« X »", texte)


if __name__ == "__main__":
    unittest.main()
