"""Raccourci de bureau sous Linux (audit du 2026-10-02, B10, et essai réel dans la
VM Ubuntu du 2026-10-06 : « Créer un raccourci sur le Bureau » ne faisait rien).

1. Le Bureau n'est pas toujours ~/Desktop : ~/Bureau en français. Écrire dans un
   dossier absent levait FileNotFoundError, avalée par le fil de l'icône.
2. La clé Exec d'un .desktop n'est pas du shell : « sh -c '...' » avec des
   apostrophes n'est pas la syntaxe de la spécification Desktop Entry.

Un analyseur de référence relit la ligne Exec comme le ferait un bureau, ce qui
éprouve l'aller-retour sur des chemins difficiles."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import raccourci_bureau as rb
import runtime


def relire_exec(valeur: str) -> list:
    """Exec relu selon la spécification : règle des chaînes (\\\\ vaut \\),
    puis découpe par espaces, guillemets doubles et barres d'échappement, et
    « %% » vaut « % »."""
    chaine = valeur.replace("\\\\", "\\")
    arguments, courant, dans, actif, i = [], [], False, False, 0
    while i < len(chaine):
        c = chaine[i]
        if dans:
            if c == "\\" and i + 1 < len(chaine):
                i += 1
                courant.append(chaine[i])
            elif c == '"':
                dans = False
            else:
                courant.append(c)
        elif c == '"':
            dans, actif = True, True
        elif c == " ":
            if actif or courant:
                arguments.append("".join(courant))
            courant, actif = [], False
        else:
            courant.append(c)
        i += 1
    if actif or courant:
        arguments.append("".join(courant))
    return [a.replace("%%", "%") for a in arguments]


class ExecConforme(unittest.TestCase):
    def aller_retour(self, arguments):
        ecrit = " ".join(rb._argument_exec(a) for a in arguments)
        self.assertEqual(relire_exec(ecrit), arguments, ecrit)
        self.assertNotIn("sh -c", ecrit)
        return ecrit

    def test_chemin_simple_reste_nu(self):
        self.assertEqual(self.aller_retour(["/usr/bin/python3", "/app/blink2video.py", "start"]),
                         "/usr/bin/python3 /app/blink2video.py start")

    def test_chemins_difficiles(self):
        for argument in ("/home/a b/blink2video", "/home/o'brien/app", 'dos"guillemet',
                         "prix$1", "100%", "100%d", "back\\slash", "a`b", "(x)&y;z|w",
                         "~/x", "tab\tici", "é-ü/日本", "a  b", "#x", "*?"):
            with self.subTest(argument=argument):
                self.aller_retour(["/opt/blink2video", argument, "start"])

    def test_pourcent_litteral_est_double(self):
        self.assertEqual(rb._argument_exec("a%b"), "a%%b")

    def test_barre_inverse_litterale_s_ecrit_avec_quatre(self):
        self.assertEqual(rb._argument_exec("a\\b"), '"a\\\\\\\\b"')

    def test_argument_vide_reste_un_argument(self):
        self.assertEqual(rb._argument_exec(""), '""')
        self.aller_retour(["/opt/app", "", "start"])


class BureauLinux(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="blink-bureau-")
        self.addCleanup(self.tmp.cleanup)
        self.accueil = Path(self.tmp.name)
        patcher = mock.patch.object(Path, "home", return_value=self.accueil)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": ""})
        env.start()
        self.addCleanup(env.stop)

    def dirs(self, texte):
        (self.accueil / ".config").mkdir(exist_ok=True)
        (self.accueil / ".config" / "user-dirs.dirs").write_text(texte, encoding="utf-8")

    def test_bureau_francais(self):
        self.dirs('XDG_DOCUMENTS_DIR="$HOME/Documents"\nXDG_DESKTOP_DIR="$HOME/Bureau"\n')
        self.assertEqual(rb._bureau_linux(), self.accueil / "Bureau")

    def test_accolades_et_chemin_absolu(self):
        self.dirs('XDG_DESKTOP_DIR="${HOME}/Schreibtisch"\n')
        self.assertEqual(rb._bureau_linux(), self.accueil / "Schreibtisch")
        ailleurs = self.accueil / "ailleurs" / "bureau"
        self.dirs(f'XDG_DESKTOP_DIR="{ailleurs}"\n')
        self.assertEqual(rb._bureau_linux(), ailleurs)

    def test_bureau_desactive_ou_relatif_retombe_sur_desktop(self):
        for ligne in ('XDG_DESKTOP_DIR="$HOME/"', 'XDG_DESKTOP_DIR="$HOME"', 'XDG_DESKTOP_DIR="bureau"'):
            with self.subTest(ligne=ligne):
                self.dirs(ligne + "\n")
                self.assertEqual(rb._bureau_linux(), self.accueil / "Desktop")

    def test_sans_fichier_de_configuration(self):
        self.assertEqual(rb._bureau_linux(), self.accueil / "Desktop")

    def test_xdg_config_home_est_respecte(self):
        autre = self.accueil / "cfg"
        autre.mkdir()
        (autre / "user-dirs.dirs").write_text('XDG_DESKTOP_DIR="$HOME/Escritorio"\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(autre)}):
            self.assertEqual(rb._bureau_linux(), self.accueil / "Escritorio")


class IconeLinux(unittest.TestCase):
    def avec_ressources(self, *fichiers):
        tmp = tempfile.TemporaryDirectory(prefix="blink-icone-")
        self.addCleanup(tmp.cleanup)
        racine = Path(tmp.name)
        for f in fichiers:
            (racine / f).parent.mkdir(parents=True, exist_ok=True)
            (racine / f).write_bytes(b"x")
        patcher = mock.patch.object(runtime, "resource_dir", return_value=racine)
        patcher.start()
        self.addCleanup(patcher.stop)
        return racine

    def test_png_a_la_racine_du_bundle(self):
        racine = self.avec_ressources("blink2video.png", "assets/blink2video.ico")
        self.assertEqual(rb._icone_linux(), racine / "blink2video.png")

    def test_png_dans_assets_depuis_les_sources(self):
        racine = self.avec_ressources("assets/blink2video.png", "assets/blink2video.ico")
        self.assertEqual(rb._icone_linux(), racine / "assets" / "blink2video.png")

    def test_repli_sur_l_ico(self):
        racine = self.avec_ressources("assets/blink2video.ico")
        self.assertEqual(rb._icone_linux(), racine / "assets" / "blink2video.ico")


class CreationLinux(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="blink-raccourci-")
        self.addCleanup(self.tmp.cleanup)
        self.accueil = Path(self.tmp.name)
        for p in (mock.patch.object(Path, "home", return_value=self.accueil),
                  mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}),
                  mock.patch.object(rb, "_ligne", return_value=["/opt/mon app/blink2video", "start"]),
                  mock.patch.object(rb, "_icone_linux", return_value=Path("/opt/mon app/icone.png")),
                  mock.patch.object(runtime, "lancer")):
            p.start()
            self.addCleanup(p.stop)
        self.sortie = mock.patch("builtins.print")
        self.sortie.start()
        self.addCleanup(self.sortie.stop)

    def test_ecrit_dans_le_bureau_francais_cree_au_besoin(self):
        (self.accueil / ".config").mkdir()
        (self.accueil / ".config" / "user-dirs.dirs").write_text(
            'XDG_DESKTOP_DIR="$HOME/Bureau"\n', encoding="utf-8")
        self.assertFalse((self.accueil / "Bureau").exists())
        self.assertEqual(rb._linux(False), 0)
        fichier = self.accueil / "Bureau" / "blink2video.desktop"
        self.assertTrue(fichier.is_file())
        self.assertFalse((self.accueil / "Desktop").exists())
        lignes = dict(l.split("=", 1) for l in fichier.read_text(encoding="utf-8").splitlines()[1:])
        self.assertEqual(relire_exec(lignes["Exec"]), ["/opt/mon app/blink2video", "start"])
        self.assertNotIn("sh -c", lignes["Exec"])
        self.assertEqual(lignes["Type"], "Application")

    def test_le_lanceur_est_marque_de_confiance_avec_la_valeur_true(self):
        # Nautilus et DING comparent à 'true' : « yes » laissait « Autoriser le
        # lancement » désactivé sur le Bureau Ubuntu.
        self.assertEqual(rb._linux(False), 0)
        fichier = self.accueil / "Desktop" / "blink2video.desktop"
        appels = [c.args[0] for c in runtime.lancer.call_args_list]
        self.assertEqual(appels, [["gio", "set", str(fichier), "metadata::trusted", "true"]])

    def test_gio_absent_ne_fait_pas_echouer_la_creation(self):
        # subprocess.run lève FileNotFoundError si le programme n'existe pas,
        # même avec check=False : le fichier est déjà écrit, c'est un succès.
        runtime.lancer.side_effect = FileNotFoundError("gio")
        self.assertEqual(rb._linux(False), 0)
        self.assertTrue((self.accueil / "Desktop" / "blink2video.desktop").is_file())

    def test_sans_configuration_ecrit_dans_desktop(self):
        self.assertEqual(rb._linux(False), 0)
        self.assertTrue((self.accueil / "Desktop" / "blink2video.desktop").is_file())

    def test_echec_d_ecriture_est_signale_et_non_leve(self):
        with mock.patch.object(Path, "write_text", side_effect=PermissionError("refuse")):
            self.assertEqual(rb._linux(False), 1)

    def test_simulation_n_ecrit_rien(self):
        self.assertEqual(rb._linux(True), 0)
        self.assertFalse((self.accueil / "Desktop").exists())


if __name__ == "__main__":
    unittest.main()
