"""Contrôles PE Win7 sans construire ni charger de DLL native."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    import verify_win7_bundle as verification
except ModuleNotFoundError as erreur:
    if erreur.name != "pefile":
        raise
    raise unittest.SkipTest("pefile absent : contrôle réservé à l'environnement de build")


def _imports(dll, *symboles):
    return SimpleNamespace(
        dll=dll,
        imports=[SimpleNamespace(name=nom) for nom in symboles],
    )


def _pe(imports=(), differes=(), machine=verification.AMD64):
    return SimpleNamespace(
        FILE_HEADER=SimpleNamespace(Machine=machine),
        DIRECTORY_ENTRY_IMPORT=imports,
        DIRECTORY_ENTRY_DELAY_IMPORT=differes,
        parse_data_directories=mock.Mock(),
        close=mock.Mock(),
    )


class LecturePETests(unittest.TestCase):
    def test_interface_historique_et_imports_differes(self):
        pe = _pe(
            [_imports(b"KERNEL32.dll", b"GetProcAddress")],
            [_imports(b"BCRYPTPRIMITIVES.DLL", b"ProcessPrng")],
        )
        with mock.patch.object(verification.pefile, "PE", return_value=pe):
            resultat = verification._lire_pe(Path("native.pyd"))
        self.assertEqual(resultat, (
            verification.AMD64, {"kernel32.dll", "bcryptprimitives.dll"}
        ))
        pe.close.assert_called_once_with()
        pe.parse_data_directories.assert_called_once_with(directories=[
            verification.pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            verification.pefile.DIRECTORY_ENTRY[
                "IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"
            ],
        ])

    def test_symboles_uniques_et_ordinals_ignores(self):
        entree = _imports(b"KERNEL32.dll", b"CreateFile2", None)
        pe = _pe([entree], [entree])
        with mock.patch.object(verification.pefile, "PE", return_value=pe):
            machine, dlls, symboles = verification._lire_pe_details(Path("x"))
        self.assertEqual(machine, verification.AMD64)
        self.assertEqual(dlls, {"kernel32.dll"})
        self.assertEqual(symboles, {("kernel32.dll", "CreateFile2")})

    def test_fichier_non_pe_conserve_le_resultat_historique(self):
        with mock.patch.object(
            verification.pefile, "PE",
            side_effect=verification.pefile.PEFormatError("pas un PE"),
        ):
            self.assertEqual(verification._lire_pe(Path("x")), (None, set()))

    def test_ferme_le_pe_meme_si_la_lecture_echoue(self):
        pe = _pe()
        pe.parse_data_directories.side_effect = RuntimeError("lecture")
        with mock.patch.object(verification.pefile, "PE", return_value=pe):
            with self.assertRaisesRegex(RuntimeError, "lecture"):
                verification._lire_pe_details(Path("x"))
        pe.close.assert_called_once_with()


class VerificationBundleTests(unittest.TestCase):
    def setUp(self):
        temporaire = tempfile.TemporaryDirectory()
        self.addCleanup(temporaire.cleanup)
        self.bundle = Path(temporaire.name)
        interne = self.bundle / "_internal"
        (interne / "certifi").mkdir(parents=True)
        (self.bundle / "blink2video.exe").touch()
        (interne / "python38.dll").touch()
        (interne / "windows7-build.txt").touch()
        (interne / "certifi" / "cacert.pem").write_bytes(b"x" * 100_000)
        self.native = interne / "native.pyd"
        self.native.touch()

    def _verifier(self, pe):
        def lire(chemin, **kwargs):
            return pe if Path(chemin) == self.native else _pe()

        with mock.patch.object(verification.pefile, "PE", side_effect=lire):
            with redirect_stdout(StringIO()) as sortie:
                erreurs = verification.verifier(self.bundle)
        return erreurs, sortie.getvalue()

    def test_chaque_api_interdite_normale_ou_differee(self):
        for symbole in verification.API_POST_WIN7:
            for differe in (False, True):
                with self.subTest(symbole=symbole, differe=differe):
                    dll = (b"BCRYPTPRIMITIVES.DLL" if symbole == "ProcessPrng"
                           else b"KERNEL32.dll")
                    entree = _imports(dll, symbole.encode("ascii"))
                    pe = _pe(differes=[entree]) if differe else _pe([entree])
                    erreurs, sortie = self._verifier(pe)
                    self.assertEqual(len(erreurs), 1, erreurs)
                    self.assertIn(str(self.native.relative_to(self.bundle)),
                                  erreurs[0])
                    self.assertIn(dll.decode().lower() + "!" + symbole,
                                  erreurs[0])
                    self.assertNotIn("OK :", sortie)

    def test_api_sets_et_kernelbase_sont_controles(self):
        for dll in (b"API-MS-Win-Core-Synch-l1-2-0.dll", b"KernelBase.dll",
                    b"EXT-MS-Win-Kernel32-Synch-l1-1-0.dll"):
            with self.subTest(dll=dll):
                erreurs, _ = self._verifier(_pe([
                    _imports(dll, b"WaitOnAddress")
                ]))
                self.assertEqual(len(erreurs), 1, erreurs)
                self.assertIn(dll.decode().lower() + "!WaitOnAddress",
                              erreurs[0])

    def test_getprocaddress_et_chaines_dynamiques_ne_sont_pas_des_imports(self):
        # Ces noms peuvent exister comme chaînes pour un GetProcAddress
        # optionnel : seules les tables PE ont autorité pour ce garde-fou.
        self.native.write_bytes(b"ProcessPrng\x00WaitOnAddress\x00CreateFile2")
        erreurs, sortie = self._verifier(_pe([
            _imports(b"KERNEL32.dll", b"GetProcAddress", b"LoadLibraryW",
                     b"GetSystemTimeAsFileTime")
        ]))
        self.assertEqual(erreurs, [])
        self.assertIn("OK :", sortie)

    def test_homonyme_dans_une_dll_applicative_autorise(self):
        erreurs, _ = self._verifier(_pe([
            _imports(b"application.dll", b"WaitOnAddress", b"ProcessPrng")
        ]))
        self.assertEqual(erreurs, [])

    def test_dll_interdite_reste_interdite_meme_par_ordinal(self):
        erreurs, _ = self._verifier(_pe(differes=[
            _imports(verification.DLL_INTERDITE.upper().encode(), None)
        ]))
        self.assertEqual(len(erreurs), 1, erreurs)
        self.assertIn(verification.DLL_INTERDITE, erreurs[0])
        self.assertIn(str(self.native.relative_to(self.bundle)), erreurs[0])

    def test_architecture_reste_controlee(self):
        erreurs, _ = self._verifier(_pe(machine=0x14C))
        self.assertEqual(len(erreurs), 1, erreurs)
        self.assertIn("non x86-64", erreurs[0])


if __name__ == "__main__":
    unittest.main()
