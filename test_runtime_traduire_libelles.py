"""Issue GitHub #6 (2026-09-13) : les messages imprimés par les commandes
(download, login, merge...) restaient tout en français, contrairement à la
page web qui suit déjà la langue choisie. `runtime.traduire()` leur donne le
même mécanisme bilingue que LIBELLES dans tray.py, indexé par
`runtime.lire_langue()` (le fichier `blink_langue.txt` laissé par le dernier
chargement de la page). blink_auth.py, blink_cli.py (en entier), blink_models.py,
blink_engine.py, merge_daily.py, maj.py et autostart.py sont les modules
convertis jusqu'ici ; tous sauf blink_auth.py et autostart.py (aucune
collision avec `_`, alias `_()` gardé) nomment leur alias local `msg()`
plutôt que `_()` puisqu'ils utilisent déjà `_` comme variable jetable
ailleurs (`for _, p in lances`, etc.)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import runtime
import blink_auth
import blink_cli
import blink_models
import blink_engine
import merge_daily
import maj
import autostart


class TestTraduireLibelles(unittest.TestCase):
    def setUp(self):
        self._dossier = tempfile.TemporaryDirectory(prefix="blink_i18n_test_")
        self._ancien_home = os.environ.get("BLINK_HOME")
        os.environ["BLINK_HOME"] = self._dossier.name

    def tearDown(self):
        if self._ancien_home is None:
            os.environ.pop("BLINK_HOME", None)
        else:
            os.environ["BLINK_HOME"] = self._ancien_home
        self._dossier.cleanup()

    def _regler_langue(self, code: str) -> None:
        (Path(self._dossier.name) / runtime.LANGUE).write_text(code, encoding="utf-8")

    def test_defaut_francais_sans_fichier_langue(self):
        self.assertEqual(runtime.lire_langue(), "fr")
        self.assertEqual(blink_auth._("session_invalide"),
                         "La session enregistrée n'est plus valide.")

    def test_bascule_en_anglais(self):
        self._regler_langue("en")
        self.assertEqual(blink_auth._("session_invalide"),
                         "The saved session is no longer valid.")

    def test_formatage_avec_valeurs(self):
        self._regler_langue("en")
        self.assertEqual(blink_auth._("code_refuse", remaining=2),
                         "Code rejected. 2 attempt(s) left.")
        self._regler_langue("fr")
        self.assertEqual(blink_auth._("code_refuse", remaining=2),
                         "Code refusé. 2 tentative(s) restante(s).")

    def test_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(blink_auth.LIBELLES["fr"]), set(blink_auth.LIBELLES["en"]))

    def test_blink_cli_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(blink_cli.LIBELLES["fr"]), set(blink_cli.LIBELLES["en"]))

    def test_blink_cli_msg_bascule_et_formate(self):
        self._regler_langue("en")
        self.assertEqual(
            blink_cli.msg("sync_module_ligne", nom="Garage", sync_id=1, network_id=2),
            "- Garage (ID 1, network 2)")
        self._regler_langue("fr")
        self.assertEqual(
            blink_cli.msg("sync_module_ligne", nom="Garage", sync_id=1, network_id=2),
            "- Garage (ID 1, réseau 2)")

    def test_blink_cli_pluriel_arrete_verbe(self):
        self._regler_langue("en")
        self.assertEqual(blink_cli.msg("arrete_verbe_code", verbe="download", code=1),
                         "Stopped: download (code 1)")
        self.assertEqual(blink_cli.msg("arrete_verbe_normal", verbe="download"),
                         "Stopped: download (normal exit)")
        self._regler_langue("fr")
        self.assertEqual(blink_cli.msg("arrete_verbe_normal", verbe="download"),
                         "Arrêté : download (fin normale)")

    def test_blink_models_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(blink_models.LIBELLES["fr"]), set(blink_models.LIBELLES["en"]))

    def test_blink_models_human_size_suit_la_langue(self):
        self._regler_langue("fr")
        self.assertEqual(blink_models.human_size(1024), "1.0 Kio")
        self._regler_langue("en")
        self.assertEqual(blink_models.human_size(1024), "1.0 KiB")

    def test_blink_models_categorie_bascule(self):
        self._regler_langue("en")
        self.assertEqual(blink_models.msg("cat_reseau"), "network")
        self._regler_langue("fr")
        self.assertEqual(blink_models.msg("cat_reseau"), "réseau")

    def test_blink_engine_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(blink_engine.LIBELLES["fr"]), set(blink_engine.LIBELLES["en"]))

    def test_blink_engine_msg_bascule_et_formate(self):
        self._regler_langue("en")
        self.assertEqual(blink_engine.msg("nouveaux_clips", n=3), "\nNew clips: 3")
        self._regler_langue("fr")
        self.assertEqual(blink_engine.msg("nouveaux_clips", n=3), "\nNouveaux clips : 3")

    def test_blink_engine_notification_singulier_pluriel(self):
        self._regler_langue("en")
        self.assertEqual(blink_engine.msg("notif_corps_singulier", n=1),
                         "1 new clip downloaded. Click to open.")
        self.assertEqual(blink_engine.msg("notif_corps_pluriel", n=3),
                         "3 new clips downloaded. Click to open.")

    def test_merge_daily_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(merge_daily.LIBELLES["fr"]), set(merge_daily.LIBELLES["en"]))

    def test_merge_daily_msg_bascule_et_formate(self):
        self._regler_langue("en")
        self.assertEqual(merge_daily.msg("mp4_invalide"), "FFmpeg did not produce a valid MP4")
        self.assertEqual(
            merge_daily.msg("jour_deja_a_jour", nom="Garage_2026-09-01.mp4", clips=5),
            "Already up to date: Garage_2026-09-01.mp4 (5 clip(s))")
        self._regler_langue("fr")
        self.assertEqual(merge_daily.msg("mp4_invalide"), "FFmpeg n'a pas produit un MP4 valide")

    def test_merge_daily_labels_periode(self):
        self._regler_langue("en")
        self.assertEqual(merge_daily.msg("label_hebdomadaires"), "Weekly")
        self.assertEqual(merge_daily.msg("label_mensuelles"), "Monthly")
        self._regler_langue("fr")
        self.assertEqual(merge_daily.msg("label_hebdomadaires"), "Hebdomadaires")
        self.assertEqual(merge_daily.msg("label_mensuelles"), "Mensuelles")

    def test_maj_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(maj.LIBELLES["fr"]), set(maj.LIBELLES["en"]))

    def test_maj_msg_bascule_et_formate(self):
        self._regler_langue("en")
        self.assertEqual(
            maj.msg("archive_chemin_dangereux", brut="../../etc/passwd"),
            "Dangerous path in the archive: '../../etc/passwd'")
        self.assertEqual(maj.msg("deja_a_jour", version="0.12.20"),
                         "blink2video 0.12.20 is up to date.")
        self._regler_langue("fr")
        self.assertEqual(maj.msg("deja_a_jour", version="0.12.20"),
                         "blink2video 0.12.20 est à jour.")

    def test_autostart_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(autostart.LIBELLES["fr"]), set(autostart.LIBELLES["en"]))

    def test_autostart_bascule_et_formate(self):
        self._regler_langue("en")
        self.assertEqual(autostart._("demarrage_retire", cible="foo.lnk"),
                         "Autostart removed: foo.lnk")
        self.assertEqual(autostart._("intitule_compte", intitule="Startup shortcuts", n=2),
                         "Startup shortcuts: 2")
        self._regler_langue("fr")
        self.assertEqual(autostart._("demarrage_deja_absent", cible="foo.lnk"),
                         "Démarrage automatique déjà absent : foo.lnk")

    def test_maj_restauration_incomplete_garde_le_message_traduit(self):
        self._regler_langue("en")
        try:
            raise maj.RestaurationIncomplete(
                maj.msg("maj_precedente_non_finalisee"))
        except maj.RestaurationIncomplete as erreur:
            self.assertEqual(
                str(erreur),
                "Previous update not finalized: backups and preparation kept.")


if __name__ == "__main__":
    unittest.main()
