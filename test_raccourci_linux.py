"""Raccourci de bureau de blink2video : ce qui lui est propre.

La pose du fichier (Bureau XDG, Exec conforme, marque de confiance, échec
d'écriture, simulation) est testée dans nico579_commons.raccourci. Ici, seulement
le câblage : le choix de l'icône et ce que blink2video demande au commun.

    python -m unittest test_raccourci_linux
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import raccourci_bureau as rb


class IconeParPlateforme(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.racine = Path(self._tmp.name)
        (self.racine / "assets").mkdir()
        for patch in (mock.patch.object(rb.runtime, "resource_dir", return_value=self.racine),
                      mock.patch.object(rb.sys, "platform", "linux")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_png_a_la_racine_du_bundle(self):
        (self.racine / "blink2video.png").write_bytes(b"x")
        (self.racine / "assets" / "blink2video.png").write_bytes(b"x")
        self.assertEqual(rb._icone(), self.racine / "blink2video.png")

    def test_png_dans_assets_depuis_les_sources(self):
        (self.racine / "assets" / "blink2video.png").write_bytes(b"x")
        self.assertEqual(rb._icone(), self.racine / "assets" / "blink2video.png")

    def test_repli_sur_l_ico(self):
        self.assertEqual(rb._icone(), self.racine / "assets" / "blink2video.ico")

    def test_windows_prend_l_ico_meme_si_un_png_existe(self):
        (self.racine / "blink2video.png").write_bytes(b"x")
        with mock.patch.object(rb.sys, "platform", "win32"):
            self.assertEqual(rb._icone(), self.racine / "assets" / "blink2video.ico")


class Cablage(unittest.TestCase):
    def test_demande_au_commun_la_commande_start_dans_un_terminal(self):
        with mock.patch.object(rb.raccourci, "creer", return_value=0) as creer, \
                mock.patch.object(rb.autostart, "commande", return_value=["/x/blink2video", "start"]) as cmd, \
                mock.patch.object(rb.runtime, "lire_langue", return_value="en"):
            self.assertEqual(rb.creer(simulation=True), 0)
        cmd.assert_called_once_with(("start", "--open-browser"))
        args, options = creer.call_args
        self.assertEqual(args[0], "blink2video")
        self.assertEqual(args[1], ["/x/blink2video", "start"])
        self.assertEqual(args[4], "Open blink2video")
        self.assertTrue(options["terminal"] and options["reduit"] and options["simulation"])
        self.assertEqual(options["langue"], "en")


if __name__ == "__main__":
    unittest.main()
