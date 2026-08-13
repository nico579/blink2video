"""Contrats de non-régression de l'étape 2 : identité et réparation.

Cette suite est entièrement locale : aucun compte, aucune socket et aucun média
réel. Les contrats absents à son introduction ont d'abord été protégés par
``expectedFailure`` ; leurs marqueurs ont été retirés avec le correctif qui les
satisfait, afin que toute régression rende désormais la suite rouge.

Exécution :

    python -B -m unittest -v test_stage2_identity.py
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


os.environ["BLINK_BOOTSTRAP"] = "none"

import blink2video as b2v  # noqa: E402 - bootstrap neutralisé avant import


INSTANT = dt.datetime(2026, 8, 13, 12, 0, tzinfo=dt.timezone.utc)


class Clip:
    """Surface commune aux objets USB et cloud dont l'identité est testée."""

    def __init__(
        self,
        identifiant=42,
        nom="jardin",
        instant=INSTANT,
        *,
        device_id="camera-7",
        network_id="reseau-1",
    ):
        self.id = identifiant
        self.name = nom
        self.created_at = instant
        self.device_id = device_id
        self.network_id = network_id
        self.size = 1
        self.download_issue = None


class Sync:
    def __init__(self, sync_id="hub-1", network_id="reseau-1"):
        self.sync_id = sync_id
        self.network_id = network_id


def arguments(output: Path, **changements):
    valeurs = dict(
        since=None,
        camera=None,
        command="download",
        output=output,
        hub=None,
        overwrite=False,
        source="cloud",
        loop=None,
    )
    valeurs.update(changements)
    return argparse.Namespace(**valeurs)


class BacASable(unittest.TestCase):
    def setUp(self):
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_stage2_")
        self.racine = Path(self.temporaire.name)
        self.home = self.racine / "home"
        self.sortie = self.racine / "clips"
        self.home.mkdir()
        self.sortie.mkdir()
        self.ancien_home = os.environ.get("BLINK_HOME")
        os.environ["BLINK_HOME"] = str(self.home)

    def tearDown(self):
        if self.ancien_home is None:
            os.environ.pop("BLINK_HOME", None)
        else:
            os.environ["BLINK_HOME"] = self.ancien_home
        self.temporaire.cleanup()

    def memoriser(
        self,
        etat: dict,
        clip: Clip,
        *,
        sync=None,
        source="usb",
        contenu=b"video",
    ) -> Path:
        cible = b2v.target_path(
            self.sortie, clip, sync=sync or Sync(), source=source,
        )
        cible.parent.mkdir(parents=True, exist_ok=True)
        cible.write_bytes(contenu)
        b2v.remember_download(
            etat,
            sync or Sync(),
            "Maison",
            clip,
            self.sortie,
            cible,
            source=source,
        )
        return cible


class TestsIdentiteEtMatching(BacASable):
    def test_T_B03_REINDEX_42_vers_99_garde_le_fichier_acquis(self):
        """Le chemin mémorisé est l'autorité après renumérotation distante."""
        etat = {"version": 1, "clips": {}}
        ancien = Clip(42)
        nouveau = Clip(99)
        ancienne_cible = self.memoriser(etat, ancien)

        nouvelle_cible = b2v.target_path(self.sortie, nouveau)
        self.assertNotEqual(ancienne_cible, nouvelle_cible)
        self.assertTrue(b2v.is_downloaded(etat, Sync(), nouveau, nouvelle_cible))
        self.assertFalse(nouvelle_cible.exists())

    def test_T_I04_USB_puis_cloud_ne_produit_qu_un_evenement(self):
        usb = Clip(42, device_id="camera-stable")
        cloud = Clip(900, device_id="camera-stable")

        inedits, doublons = b2v.rapprocher([usb], [cloud])

        self.assertEqual(inedits, [])
        self.assertEqual(doublons, [cloud])

    def test_T_I04_cloud_puis_USB_ne_produit_qu_un_evenement(self):
        """L'identité du registre est indépendante de l'ordre des sources."""
        etat = {"version": 1, "clips": {}}
        cloud = Clip(900, device_id="camera-stable")
        usb = Clip(42, device_id="camera-stable")
        self.memoriser(etat, cloud, sync=Sync("cloud", "reseau-1"), source="cloud")

        self.assertTrue(
            b2v.is_downloaded(
                etat,
                Sync("hub-1", "reseau-1"),
                usb,
                b2v.target_path(self.sortie, usb),
            )
        )

    def test_T_I21_deux_evenements_legitimes_proches_restent_deux(self):
        premier = Clip(1, instant=INSTANT, device_id="camera-stable")
        second = Clip(
            2,
            instant=INSTANT + dt.timedelta(seconds=1),
            device_id="camera-stable",
        )

        self.assertNotEqual(
            b2v.state_key(Sync(), premier), b2v.state_key(Sync(), second)
        )
        self.assertNotEqual(
            b2v.target_path(self.sortie, premier),
            b2v.target_path(self.sortie, second),
        )

    def test_T_I21_un_match_local_n_est_consomme_qu_une_fois(self):
        local = Clip(1, device_id="camera-stable")
        cloud_exact = Clip(2, device_id="camera-stable")
        cloud_proche = Clip(
            3,
            instant=INSTANT + dt.timedelta(seconds=1),
            device_id="camera-stable",
        )

        inedits, doublons = b2v.rapprocher(
            [local], [cloud_proche, cloud_exact], tolerance=2
        )

        self.assertEqual(doublons, [cloud_exact])
        self.assertEqual(inedits, [cloud_proche])
        self.assertEqual(
            b2v._apparier_evenements([local], [cloud_proche, cloud_exact], 2),
            [(0, 1)],
        )

    def test_T_I21_appariement_conserve_la_cardinalite_maximale(self):
        locaux = [
            Clip(1, instant=INSTANT, device_id="camera-stable"),
            Clip(
                2,
                instant=INSTANT + dt.timedelta(seconds=3),
                device_id="camera-stable",
            ),
        ]
        cloud = [
            Clip(
                3,
                instant=INSTANT + dt.timedelta(seconds=2),
                device_id="camera-stable",
            ),
            Clip(
                4,
                instant=INSTANT + dt.timedelta(seconds=4),
                device_id="camera-stable",
            ),
        ]

        inedits, doublons = b2v.rapprocher(locaux, cloud, tolerance=2)

        self.assertEqual(inedits, [])
        self.assertEqual(doublons, cloud)
        self.assertEqual(b2v._apparier_evenements(locaux, cloud, 2), [(0, 0), (1, 1)])

    def test_T_I16_camera_renommee_device_id_stable_ne_duplique_pas(self):
        ancien_nom = Clip(1, nom="Jardin", device_id="camera-stable")
        nouveau_nom = Clip(2, nom="Terrasse", device_id="camera-stable")

        inedits, doublons = b2v.rapprocher([ancien_nom], [nouveau_nom])

        self.assertEqual(inedits, [])
        self.assertEqual(doublons, [nouveau_nom])

    def test_T_I16_A_slash_B_et_A_underscore_B_restent_distincts(self):
        slash = Clip(1, nom="A/B", device_id="camera-slash")
        underscore = Clip(2, nom="A_B", device_id="camera-underscore")

        self.assertEqual(b2v.safe_name(slash.name), b2v.safe_name(underscore.name))
        self.assertNotEqual(
            b2v.state_key(Sync(), slash), b2v.state_key(Sync(), underscore)
        )
        inedits, doublons = b2v.rapprocher([slash], [underscore])
        self.assertEqual(inedits, [underscore])
        self.assertEqual(doublons, [])

    def test_T_I20_multi_reseaux_ne_collisionne_pas(self):
        premier = Clip(1, network_id="reseau-1")
        second = Clip(2, network_id="reseau-2")

        self.assertNotEqual(
            b2v.state_key(Sync("cloud", "reseau-1"), premier),
            b2v.state_key(Sync("cloud", "reseau-2"), second),
        )
        inedits, doublons = b2v.rapprocher([premier], [second])
        self.assertEqual(inedits, [second])
        self.assertEqual(doublons, [])

    def test_T_I20_multi_hubs_ne_collisionne_pas(self):
        clip = Clip(1, network_id="reseau-commun")
        hub_a = Sync("hub-A", "reseau-commun")
        hub_b = Sync("hub-B", "reseau-commun")

        self.assertNotEqual(
            b2v.state_key(hub_a, clip),
            b2v.state_key(hub_b, clip),
        )
        self.assertNotEqual(
            b2v.target_path(self.sortie, clip, sync=hub_a, source="usb"),
            b2v.target_path(self.sortie, clip, sync=hub_b, source="usb"),
        )


class TestsRegistreRobuste(BacASable):
    @staticmethod
    def _entry(clip, chemin, *, bytes_=1, excluded=False):
        return {
            "camera": clip.name,
            "created_at": clip.created_at.isoformat(),
            "path": chemin,
            "bytes": bytes_,
            "source": "usb",
            "excluded": excluded,
        }

    def test_T_B03_entry_path_hostile_n_est_jamais_acquis(self):
        clip = Clip()
        cible = b2v.target_path(self.sortie, clip)
        hors_racine = self.racine / "hors-racine.mp4"
        hors_racine.write_bytes(b"x")
        (self.sortie / "un-dossier").mkdir()
        chemins = {
            "absolu": str(hors_racine.resolve()),
            "traversal": "../hors-racine.mp4",
            "repertoire": "un-dossier",
            "vide": "",
        }

        for libelle, chemin in chemins.items():
            with self.subTest(cas=libelle):
                etat = {
                    "version": 2,
                    "clips": {
                        b2v.state_key(Sync(), clip): self._entry(clip, chemin),
                    },
                }
                self.assertFalse(b2v.is_downloaded(etat, Sync(), clip, cible))

    def test_T_I21_fallback_registre_est_consomme_une_seule_fois(self):
        archive = self.sortie / "archive.mp4"
        archive.write_bytes(b"archive")
        premier = Clip(101, instant=INSTANT)
        second = Clip(102, instant=INSTANT + dt.timedelta(seconds=1))
        etat = {
            "version": 2,
            "clips": {
                "ancienne-cle": self._entry(
                    premier,
                    archive.relative_to(self.sortie).as_posix(),
                    bytes_=archive.stat().st_size,
                ),
            },
        }
        consommees = set()

        self.assertTrue(
            b2v.is_downloaded(
                etat,
                Sync(),
                premier,
                b2v.target_path(self.sortie, premier),
                consommees,
            )
        )
        self.assertFalse(
            b2v.is_downloaded(
                etat,
                Sync(),
                second,
                b2v.target_path(self.sortie, second),
                consommees,
            )
        )
        self.assertEqual(consommees, {"ancienne-cle"})

    def test_T_B03_tombstone_ancienne_gagne_sur_cle_exacte_normale(self):
        clip = Clip()
        archive = self.sortie / "archive.mp4"
        archive.write_bytes(b"archive")
        etat = {
            "version": 2,
            "clips": {
                b2v.state_key(Sync(), clip): self._entry(
                    clip,
                    archive.relative_to(self.sortie).as_posix(),
                    bytes_=archive.stat().st_size,
                ),
                "ancienne-tombstone": self._entry(
                    clip, "absent.mp4", excluded=True,
                ),
            },
        }

        correspondance = b2v._apparier_registre(etat, Sync(), [clip])[0]

        self.assertEqual(correspondance[0], "ancienne-tombstone")
        self.assertIs(correspondance[1]["excluded"], True)

    def test_T_I20_lookup_cloud_utilise_source_et_remote_id(self):
        etat = {"version": 2, "clips": {}}
        sync = b2v._HubCloud("reseau-1")
        premier = Clip(101)
        second = Clip(202)
        self.memoriser(etat, premier, sync=sync, source="cloud")
        self.memoriser(etat, second, sync=sync, source="cloud")

        cle, entree = b2v._trouver_entree(
            etat, sync, second, source="cloud",
        )

        self.assertEqual(cle, b2v.state_key(sync, second, "cloud"))
        self.assertEqual(entree["remote_id"], "202")

    def test_T_B04_merge_v2_est_monotone_et_preserve_tombstone_disque(self):
        clip = Clip()
        registre = self.sortie / b2v.STATE_FILENAME
        registre.write_text(
            json.dumps(
                {
                    "version": 2,
                    "clips": {
                        "commun": self._entry(
                            clip, "commun.mp4", excluded=True
                        ),
                        "seulement-disque": self._entry(clip, "disque.mp4"),
                    },
                }
            ),
            encoding="utf-8",
        )
        etat_perime = {
            "version": 1,
            "clips": {
                "commun": self._entry(clip, "commun.mp4", excluded=False),
                "seulement-memoire": self._entry(clip, "memoire.mp4"),
            },
        }

        b2v._ecrire_registre(registre, etat_perime)
        fusion = json.loads(registre.read_text(encoding="utf-8"))

        self.assertEqual(fusion["version"], 2)
        self.assertEqual(
            set(fusion["clips"]),
            {"commun", "seulement-disque", "seulement-memoire"},
        )
        self.assertIs(fusion["clips"]["commun"]["excluded"], True)


class TestsReparationCloud(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_stage2_async_")
        self.racine = Path(self.temporaire.name)
        self.home = self.racine / "home"
        self.sortie = self.racine / "clips"
        self.home.mkdir()
        self.sortie.mkdir()
        self.ancien_home = os.environ.get("BLINK_HOME")
        os.environ["BLINK_HOME"] = str(self.home)

    async def asyncTearDown(self):
        if self.ancien_home is None:
            os.environ.pop("BLINK_HOME", None)
        else:
            os.environ["BLINK_HOME"] = self.ancien_home
        self.temporaire.cleanup()

    @staticmethod
    def _entry(clip, chemin="ancien.mp4", *, excluded=False):
        return {
            "camera": clip.name,
            "created_at": clip.created_at.isoformat(),
            "path": chemin,
            "source": "cloud",
            "excluded": excluded,
        }

    async def _executer(self, clip, entree):
        (self.sortie / b2v.STATE_FILENAME).write_text(
            json.dumps({"version": 1, "clips": {"ancienne-cle": entree}}),
            encoding="utf-8",
        )

        appels = []

        async def telecharger(_blink, cible):
            appels.append(cible)
            cible.write_bytes(b"repare")
            return True

        clip.download_to = telecharger
        with mock.patch.object(
            b2v, "read_cloud_manifest", new=mock.AsyncMock(return_value=[clip])
        ), contextlib.redirect_stdout(io.StringIO()):
            resultat = await b2v.traiter_cloud(
                object(), arguments(self.sortie), []
            )
        return resultat, appels

    async def test_T_B03_MISSING_fichier_absent_normal_est_repare(self):
        clip = Clip()

        resultat, appels = await self._executer(clip, self._entry(clip))

        self.assertEqual(resultat.downloaded, 1)
        self.assertEqual(len(appels), 1)
        self.assertTrue(b2v.target_path(self.sortie, clip).is_file())

    async def test_T_B03_EXCLUDED_fichier_absent_exclu_est_ignore(self):
        clip = Clip()

        resultat, appels = await self._executer(
            clip, self._entry(clip, excluded=True)
        )

        self.assertEqual(resultat.downloaded, 0)
        self.assertEqual(resultat.skipped, 1)
        self.assertEqual(appels, [])
        self.assertFalse(b2v.target_path(self.sortie, clip).exists())

    async def test_T_B03_EXCLUDED_overwrite_cloud_reste_ignore(self):
        clip = Clip()

        async def ne_doit_pas_telecharger(_blink, cible):
            cible.write_bytes(b"interdit")
            return True

        clip.download_to = ne_doit_pas_telecharger
        (self.sortie / b2v.STATE_FILENAME).write_text(
            json.dumps(
                {
                    "version": 1,
                    "clips": {
                        "tombstone": self._entry(clip, excluded=True),
                    },
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(
            b2v, "read_cloud_manifest", new=mock.AsyncMock(return_value=[clip])
        ), contextlib.redirect_stdout(io.StringIO()):
            resultat = await b2v.traiter_cloud(
                object(), arguments(self.sortie, overwrite=True), []
            )

        self.assertEqual(resultat.downloaded, 0)
        self.assertEqual(resultat.skipped, 1)
        self.assertFalse(b2v.target_path(self.sortie, clip).exists())

    async def test_T_I05_taille_divergente_n_est_pas_readoptee(self):
        clip = Clip()
        sync_cloud = Sync(clip.network_id, clip.network_id)
        cible = b2v.target_path(
            self.sortie, clip, sync=sync_cloud, source="cloud"
        )
        cible.parent.mkdir(parents=True)
        cible.write_bytes(b"tronque")
        entree = self._entry(
            clip,
            cible.relative_to(self.sortie).as_posix(),
        )
        entree["bytes"] = 1000
        (self.sortie / b2v.STATE_FILENAME).write_text(
            json.dumps({"version": 2, "clips": {"incomplet": entree}}),
            encoding="utf-8",
        )
        appels = []

        async def reparer(_blink, partiel):
            appels.append(partiel)
            partiel.write_bytes(b"video-reparee")
            return True

        clip.download_to = reparer
        with mock.patch.object(
            b2v, "read_cloud_manifest", new=mock.AsyncMock(return_value=[clip])
        ), contextlib.redirect_stdout(io.StringIO()):
            resultat = await b2v.traiter_cloud(
                object(), arguments(self.sortie), []
            )

        self.assertEqual(resultat.downloaded, 1)
        self.assertEqual(resultat.adopted, 0)
        self.assertEqual(len(appels), 1)
        self.assertEqual(cible.read_bytes(), b"video-reparee")

    async def test_T_I16_filtre_cloud_A_slash_B_exclut_A_underscore_B(self):
        slash = Clip(1, nom="A/B", device_id="camera-slash")
        underscore = Clip(2, nom="A_B", device_id="camera-underscore")
        appels = []
        original = b2v.rapprocher

        def rapprocher_trace(locaux, cloud, tolerance=2):
            appels.append(list(cloud))
            return original(locaux, cloud, tolerance)

        with mock.patch.object(
            b2v,
            "read_cloud_manifest",
            new=mock.AsyncMock(return_value=[slash, underscore]),
        ), mock.patch.object(b2v, "rapprocher", side_effect=rapprocher_trace), \
             contextlib.redirect_stdout(io.StringIO()):
            await b2v.traiter_cloud(
                object(),
                arguments(self.sortie, command="list", camera="A/B"),
                [],
            )

        self.assertGreaterEqual(len(appels), 1)
        self.assertTrue(all(clips == [slash] for clips in appels))

    async def test_T_B03_EXCLUDED_overwrite_USB_reste_ignore(self):
        clip = Clip()
        (self.sortie / b2v.STATE_FILENAME).write_text(
            json.dumps(
                {
                    "version": 1,
                    "clips": {
                        "tombstone": self._entry(clip, excluded=True),
                    },
                }
            ),
            encoding="utf-8",
        )
        telecharger = mock.AsyncMock(return_value="failed")
        with mock.patch.object(
            b2v, "read_local_manifest", new=mock.AsyncMock(return_value=[clip])
        ), mock.patch.object(b2v, "download_clip", new=telecharger), \
             mock.patch.object(b2v.runtime, "travail"), \
             mock.patch.object(b2v.runtime, "marquer"), \
             mock.patch.object(b2v.runtime, "toast"), \
             contextlib.redirect_stdout(io.StringIO()):
            code = await b2v.un_passage(
                object(),
                arguments(self.sortie, source="usb", overwrite=True),
                [("Maison", Sync())],
            )

        self.assertEqual(code, 0)
        telecharger.assert_not_awaited()

    async def test_T_I20_cloud_utilise_le_network_id_de_chaque_clip(self):
        premier = Clip(1, instant=INSTANT, network_id="reseau-A")
        second = Clip(
            2,
            instant=INSTANT + dt.timedelta(minutes=1),
            network_id="reseau-B",
        )

        async def telecharger(_blink, cible):
            cible.write_bytes(b"video")
            return True

        premier.download_to = telecharger
        second.download_to = telecharger
        with mock.patch.object(
            b2v,
            "read_cloud_manifest",
            new=mock.AsyncMock(return_value=[premier, second]),
        ), contextlib.redirect_stdout(io.StringIO()):
            resultat = await b2v.traiter_cloud(
                object(), arguments(self.sortie), [("Maison", Sync("hub-1", "X"))]
            )

        self.assertEqual(resultat.downloaded, 2)
        registre = b2v.load_download_state(self.sortie)
        reseaux = {entree.get("network_id") for entree in registre["clips"].values()}
        self.assertEqual(reseaux, {"reseau-A", "reseau-B"})


class TestsMigrationV1(BacASable):
    def _etat_v1(self, archive: Path):
        clip = Clip(42, nom="Ancien nom", device_id="camera-stable")
        cle = f"hub-1:{b2v.safe_name(clip.name)}:{clip.created_at.isoformat()}"
        return {
            "version": 1,
            "clips": {
                cle: {
                    "hub": "Maison",
                    "camera": clip.name,
                    "created_at": clip.created_at.isoformat(),
                    "path": archive.relative_to(self.sortie).as_posix(),
                    "bytes": archive.stat().st_size,
                    "source": "usb",
                }
            },
        }

    def test_T_MIGRATION_V1_est_idempotente_et_archive_preservee(self):
        archive = self.sortie / "Ancien nom" / "2026-08" / "archive-42.mp4"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"archive-intacte")
        etat_v1 = self._etat_v1(archive)
        registre = self.sortie / b2v.STATE_FILENAME
        original = json.dumps(etat_v1).encode("utf-8")
        registre.write_bytes(original)

        premier = b2v.load_download_state(self.sortie)
        b2v.save_download_state(self.sortie, premier)
        apres_premier = registre.read_bytes()
        second = b2v.load_download_state(self.sortie)
        b2v.save_download_state(self.sortie, second)
        apres_second = registre.read_bytes()

        self.assertEqual(premier["version"], 2)
        self.assertEqual(second, premier)
        self.assertEqual(apres_second, apres_premier)
        self.assertEqual(archive.read_bytes(), b"archive-intacte")
        sauvegarde = self.sortie / b2v.STATE_V1_BACKUP_FILENAME
        self.assertEqual(sauvegarde.read_bytes(), original)
        registre.write_bytes(sauvegarde.read_bytes())
        restaure = b2v.load_download_state(self.sortie)
        self.assertEqual(restaure["version"], 2)
        self.assertEqual(set(restaure["clips"]), set(etat_v1["clips"]))
        self.assertEqual(
            next(iter(second["clips"].values()))["path"],
            archive.relative_to(self.sortie).as_posix(),
        )

    def test_T_MIGRATION_V1_echec_backup_annule_ecriture_v2(self):
        archive = self.sortie / "archive.mp4"
        archive.write_bytes(b"archive")
        registre = self.sortie / b2v.STATE_FILENAME
        original = json.dumps(self._etat_v1(archive)).encode("utf-8")
        registre.write_bytes(original)
        etat = b2v.load_download_state(self.sortie)

        with mock.patch.object(Path, "write_bytes", side_effect=OSError("disque")):
            with self.assertRaisesRegex(OSError, "migration v2 annulée"):
                b2v.save_download_state(self.sortie, etat)

        self.assertEqual(registre.read_bytes(), original)
        self.assertFalse(
            (self.sortie / b2v.STATE_V1_BACKUP_FILENAME).exists()
        )


class TestsNomsDeFichiers(BacASable):
    def test_T_I16_noms_reserves_windows_sont_neutralises(self):
        reserves = ["CON", "PRN", "AUX", "NUL", "COM1", "LPT9", "con.txt"]

        for nom in reserves:
            with self.subTest(nom=nom):
                composant = b2v.safe_name(nom)
                racine = composant.split(".", 1)[0].casefold()
                self.assertNotIn(
                    racine,
                    {"con", "prn", "aux", "nul", "com1", "lpt9"},
                )

    def test_T_I16_separateurs_et_path_traversal_restent_dans_output(self):
        noms = ["../secret", r"..\secret", "dossier/fichier", r"dossier\fichier"]

        for position, nom in enumerate(noms):
            with self.subTest(nom=nom):
                clip = Clip(position, nom=nom, device_id=f"camera-{position}")
                cible = b2v.target_path(self.sortie, clip)
                cible.resolve().relative_to(self.sortie.resolve())
                relatif = cible.relative_to(self.sortie)
                self.assertEqual(len(relatif.parts), 3)
                self.assertNotIn("..", relatif.parts)
                self.assertNotIn("/", b2v.safe_name(nom))
                self.assertNotIn("\\", b2v.safe_name(nom))

    def test_T_I16_composants_et_chemin_total_sont_bornes(self):
        clip = Clip(
            "id-" + "9" * 500,
            nom="caméra-" + "é" * 500,
            device_id="device-" + "x" * 500,
        )

        cible = b2v.target_path(self.sortie, clip)

        self.assertTrue(all(len(part.encode("utf-8")) <= 255 for part in cible.parts))
        self.assertLessEqual(len(str(cible)), 240)


if __name__ == "__main__":
    unittest.main(verbosity=2)
