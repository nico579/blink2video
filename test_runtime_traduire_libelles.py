"""Issue GitHub #6 (2026-09-13) : les messages imprimés par les commandes
(download, login, merge...) restaient tout en français, contrairement à la
page web qui suit déjà la langue choisie. `runtime.traduire()` leur donne le
même mécanisme bilingue que LIBELLES dans tray.py, indexé par
`runtime.lire_langue()` (le fichier `blink_langue.txt` laissé par le dernier
chargement de la page). blink_auth.py et le flux login/download de
blink_cli.py (async def main) sont les deux premiers convertis ; blink_cli.py
nomme son alias local `msg()` plutôt que `_()` puisque ce fichier utilise déjà
`_` comme variable jetable ailleurs (`for _, p in lances`, etc.)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import runtime
import blink_auth
import blink_cli


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


if __name__ == "__main__":
    unittest.main()
