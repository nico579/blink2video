"""Garde-fous de sécurité de la mise à jour et du déploiement.

Les archives et réponses HTTP sont construites localement : aucun test de ce
module n'accède au réseau et aucun ne touche au dépôt Git qui l'héberge.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import deploy
import maj


class _ReponseHTTP:
    def __init__(self, contenu: bytes, url: str, taille_http=None):
        self._source = io.BytesIO(contenu)
        self._url = url
        self.headers = {}
        if taille_http is not None:
            self.headers["Content-Length"] = str(taille_http)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self._url

    def read(self, taille=-1):
        return self._source.read(taille)


class SecuriteMiseAJourTests(unittest.TestCase):
    def setUp(self):
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_maj_secure_")
        self.racine = Path(self.temporaire.name)

    def tearDown(self):
        self.temporaire.cleanup()

    def test_zip_nominal_est_extrait_manuellement(self):
        archive = self.racine / "bundle.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as sortie:
            sortie.writestr("blink2video/blink2video", b"programme")

        dossier = maj._extraire(archive, self.racine / "contenu")

        self.assertEqual(dossier.name, "blink2video")
        self.assertEqual((dossier / "blink2video").read_bytes(), b"programme")

    def test_zip_refuse_traversee_et_necrit_pas_hors_racine(self):
        archive = self.racine / "traversee.zip"
        with zipfile.ZipFile(archive, "w") as sortie:
            sortie.writestr("../echappe", b"attaque")

        with self.assertRaises(OSError):
            maj._extraire(archive, self.racine / "contenu")

        self.assertFalse((self.racine / "echappe").exists())

    def test_zip_refuse_les_liens_symboliques(self):
        archive = self.racine / "lien.zip"
        lien = zipfile.ZipInfo("blink2video/lien")
        lien.create_system = 3
        lien.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(archive, "w") as sortie:
            sortie.writestr(lien, "../../echappe")

        with self.assertRaises(OSError):
            maj._extraire(archive, self.racine / "contenu")

    def test_tar_refuse_les_liens_et_necrit_pas_hors_racine(self):
        archive = self.racine / "lien.tar.gz"
        with tarfile.open(archive, "w:gz") as sortie:
            dossier = tarfile.TarInfo("blink2video")
            dossier.type = tarfile.DIRTYPE
            sortie.addfile(dossier)
            lien = tarfile.TarInfo("blink2video/lien")
            lien.type = tarfile.SYMTYPE
            lien.linkname = "../../echappe"
            sortie.addfile(lien)

        with self.assertRaises(OSError):
            maj._extraire(archive, self.racine / "contenu")

        self.assertFalse((self.racine / "echappe").exists())

    # Issue #21 : les bundles PyInstaller publiés pour Linux et macOS
    # contiennent des liens relatifs internes (bibliothèques de Pillow,
    # framework Python). Tous refusés, ils faisaient échouer chaque mise à jour.
    def _tar_avec_lien(self, cible_lien: str, *, sous_le_lien: bool = False) -> Path:
        archive = self.racine / "bundle.tar.gz"
        with tarfile.open(archive, "w:gz") as sortie:
            for nom in ("blink2video", "blink2video/_internal",
                        "blink2video/_internal/pillow.libs"):
                dossier = tarfile.TarInfo(nom)
                dossier.type = tarfile.DIRTYPE
                sortie.addfile(dossier)
            contenu = b"bibliotheque"
            fichier = tarfile.TarInfo("blink2video/_internal/pillow.libs/libwebp.so.7")
            fichier.size = len(contenu)
            sortie.addfile(fichier, io.BytesIO(contenu))
            lien = tarfile.TarInfo("blink2video/_internal/libwebp.so.7")
            lien.type = tarfile.SYMTYPE
            lien.linkname = cible_lien
            sortie.addfile(lien)
            if sous_le_lien:
                cache = tarfile.TarInfo("blink2video/_internal/libwebp.so.7/cache")
                cache.size = 0
                sortie.addfile(cache, io.BytesIO(b""))
        return archive

    @unittest.skipIf(os.name == "nt", "créer un lien exige un privilège sous Windows")
    def test_tar_accepte_les_liens_internes_du_bundle(self):
        dossier = maj._extraire(self._tar_avec_lien("pillow.libs/libwebp.so.7"),
                                self.racine / "contenu")

        lien = dossier / "_internal" / "libwebp.so.7"
        self.assertTrue(lien.is_symlink())
        self.assertEqual(os.readlink(lien), "pillow.libs/libwebp.so.7")
        self.assertEqual(lien.read_bytes(), b"bibliotheque")

    @unittest.skipIf(os.name == "nt", "créer un lien exige un privilège sous Windows")
    def test_zip_accepte_les_liens_internes_du_bundle(self):
        archive = self.racine / "bundle.zip"
        with zipfile.ZipFile(archive, "w") as sortie:
            sortie.writestr("blink2video/_internal/Python.framework/Versions/3.12/Python",
                            b"python")
            lien = zipfile.ZipInfo("blink2video/_internal/Python.framework/Versions/Current")
            lien.create_system = 3
            lien.external_attr = (stat.S_IFLNK | 0o777) << 16
            sortie.writestr(lien, "3.12")

        dossier = maj._extraire(archive, self.racine / "contenu")

        courant = dossier / "_internal" / "Python.framework" / "Versions" / "Current"
        self.assertTrue(courant.is_symlink())
        self.assertEqual((courant / "Python").read_bytes(), b"python")

    def test_lien_absolu_refuse(self):
        with self.assertRaises(OSError):
            maj._extraire(self._tar_avec_lien("/etc/passwd"), self.racine / "contenu")

    def test_lien_ne_peut_pas_servir_de_dossier(self):
        # Sinon un membre rangé « sous » le lien s'écrirait là où il pointe.
        with self.assertRaises(OSError):
            maj._extraire(self._tar_avec_lien("pillow.libs", sous_le_lien=True),
                          self.racine / "contenu")

    @unittest.skipUnless(os.name == "nt", "politique propre à Windows")
    def test_liens_refuses_sous_windows(self):
        with self.assertRaises(OSError):
            maj._extraire(self._tar_avec_lien("pillow.libs/libwebp.so.7"),
                          self.racine / "contenu")

    @unittest.skipIf(os.name == "nt", "créer un lien exige un privilège sous Windows")
    def test_pose_conserve_les_liens_du_bundle(self):
        source = self.racine / "neuf" / "_internal"
        (source / "pillow.libs").mkdir(parents=True)
        (source / "pillow.libs" / "libwebp.so.7").write_bytes(b"bibliotheque")
        os.symlink("pillow.libs/libwebp.so.7", source / "libwebp.so.7")

        maj._poser(source, self.racine / "installe" / "_internal")

        pose = self.racine / "installe" / "_internal" / "libwebp.so.7"
        self.assertTrue(pose.is_symlink())
        self.assertEqual(pose.read_bytes(), b"bibliotheque")

    def test_archive_decompressee_est_bornee_avant_ecriture(self):
        archive = self.racine / "bombe.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as sortie:
            sortie.writestr("blink2video/gros", b"1234")

        with mock.patch.object(maj, "MAX_EXTRACTED_BYTES", 3), \
                self.assertRaises(OSError):
            maj._extraire(archive, self.racine / "contenu")

        self.assertFalse((self.racine / "contenu" / "blink2video" / "gros").exists())

    def test_telechargement_verifie_taille_et_sha_avant_installation(self):
        contenu = b"archive authentifiee"
        url = "https://github.com/nico579/blink2video/releases/download/v1.0.0/a.zip"
        reponse = _ReponseHTTP(contenu, url, len(contenu))
        destination = self.racine / "a.zip"

        with mock.patch.object(maj.urllib.request, "urlopen", return_value=reponse), \
                mock.patch.object(maj.runtime, "travail"):
            maj._telecharger(url, destination, len(contenu),
                             hashlib.sha256(contenu).hexdigest())

        self.assertEqual(destination.read_bytes(), contenu)

    def test_telechargement_efface_le_partiel_si_sha_incorrect(self):
        contenu = b"archive substituee"
        url = "https://github.com/nico579/blink2video/releases/download/v1.0.0/a.zip"
        reponse = _ReponseHTTP(contenu, url, len(contenu))
        destination = self.racine / "a.zip"

        with mock.patch.object(maj.urllib.request, "urlopen", return_value=reponse), \
                mock.patch.object(maj.runtime, "travail"), \
                self.assertRaises(OSError):
            maj._telecharger(url, destination, len(contenu), "0" * 64)

        self.assertFalse(destination.exists())

    def test_version_du_binaire_doit_correspondre_exactement(self):
        dossier = self.racine / "bundle"
        dossier.mkdir()
        maj._executable(dossier).write_bytes(b"")
        trop_longue = SimpleNamespace(
            returncode=0, stdout="blink2video 1.2.3-pirate\n", stderr="")
        exacte = SimpleNamespace(returncode=0, stdout="blink2video 1.2.3\n", stderr="")

        with mock.patch.object(maj.runtime, "lancer", return_value=trop_longue), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(maj._verifier(dossier, "1.2.3"))
        with mock.patch.object(maj.runtime, "lancer", return_value=exacte), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(maj._verifier(dossier, "1.2.3"))

    def test_selection_archive_associe_l_empreinte_sans_choisir_le_sidecar(self):
        assets = [
            {"name": "blink2video-linux-x86_64.tar.gz.sha256",
             "browser_download_url": "https://github.com/checksum", "size": 100},
            {"name": "blink2video-linux-x86_64.tar.gz",
             "browser_download_url": "https://github.com/archive", "size": 123},
        ]
        with mock.patch.object(maj.sys, "platform", "linux"), \
                mock.patch.object(maj.platform, "machine", return_value="x86_64"):
            choisie = maj._archive_de_ce_systeme(assets)

        self.assertEqual(choisie["nom"], "blink2video-linux-x86_64.tar.gz")
        self.assertEqual(choisie["checksum_url"], "https://github.com/checksum")

    def test_cache_ne_peut_pas_pointer_vers_un_autre_depot_github(self):
        nom = "blink2video-linux-x86_64.tar.gz"
        self.assertTrue(maj._url_release_officielle(
            f"https://github.com/nico579/blink2video/releases/download/v1.2.3/{nom}",
            nom,
        ))
        self.assertFalse(maj._url_release_officielle(
            f"https://github.com/attaquant/blink2video/releases/download/v1.2.3/{nom}",
            nom,
        ))
        self.assertFalse(maj._url_release_officielle(
            f"https://github.com/nico579/blink2video/releases/download/v1.2.3/autre.zip",
            nom,
        ))


class SecuriteDeploiementTests(unittest.TestCase):
    def test_remote_officiel_est_compare_exactement(self):
        self.assertTrue(deploy._remote_officiel(
            "https://github.com/nico579/blink2video.git"))
        self.assertTrue(deploy._remote_officiel(
            "git@github.com:nico579/blink2video.git"))
        self.assertTrue(deploy._remote_officiel(
            "ssh://git@github.com/nico579/blink2video.git"))
        self.assertFalse(deploy._remote_officiel(
            "https://github.com/attaquant/blink2video.git"))
        self.assertFalse(deploy._remote_officiel(
            "https://github.com/nico579/blink2video.git@evil.invalid/depot"))

    def test_deploiement_refuse_une_autre_branche(self):
        with mock.patch.object(deploy, "_sortie_git", return_value="feature"), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit):
            deploy.verifier_depot()

    def test_deploiement_refuse_un_remote_different(self):
        sorties = [
            "main",
            "https://github.com/attaquant/blink2video.git",
            "https://github.com/nico579/blink2video.git",
        ]
        with mock.patch.object(deploy, "_sortie_git", side_effect=sorties), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit):
            deploy.verifier_depot()

    def test_deploiement_refuse_un_sha_distant_different(self):
        sha_local = "a" * 40
        sorties = [
            "main",
            "https://github.com/nico579/blink2video.git",
            "https://github.com/nico579/blink2video.git",
            sha_local,
        ]
        with mock.patch.object(deploy, "_sortie_git", side_effect=sorties), \
                mock.patch.object(deploy, "_sha_remote", return_value="b" * 40), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit):
            deploy.verifier_depot()

    def test_dry_run_ne_mute_jamais_index(self):
        appels = []

        def faux_git(*args, **_kwargs):
            appels.append(args)
            stdout = " M maj.py\n?? nouveau" if args[:2] == ("status", "--short") else ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        with mock.patch.object(deploy, "git", side_effect=faux_git), \
                contextlib.redirect_stdout(io.StringIO()):
            changements = deploy.compute_diff(dry_run=True)

        self.assertTrue(changements)
        self.assertNotIn(("add", "-A"), appels)

    def test_push_vise_explicitement_main_et_verifie_son_sha(self):
        sha = "c" * 40
        appels = []

        def faux_git(*args, **_kwargs):
            appels.append(args)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(deploy, "git", side_effect=faux_git), \
                mock.patch.object(deploy, "_sortie_git", return_value=sha), \
                mock.patch.object(deploy, "_sha_remote", return_value=sha), \
                contextlib.redirect_stdout(io.StringIO()):
            obtenu = deploy.commit_and_push("message", "")

        self.assertEqual(obtenu, sha)
        self.assertIn(("push", "origin", "HEAD:refs/heads/main"), appels)


if __name__ == "__main__":
    unittest.main()
