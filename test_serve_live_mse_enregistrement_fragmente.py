"""Bug rapporté le 2026-09-06 : démarrer l'enregistrement du direct MSE en
cours de route (pas dès le premier bloc) pouvait couper un mdat en deux, le
fichier obtenu manquant le moof de tête - illisible à la relecture."""

from __future__ import annotations

import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-live-mse-enreg-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import serve  # noqa: E402 - bootstrap neutralisé avant import


def _boite(genre: bytes, charge: bytes) -> bytes:
    return (8 + len(charge)).to_bytes(4, "big") + genre + charge


def _segment_synthetique() -> bytes:
    avcc = _boite(b"avcC", bytes([1, 0x64, 0x00, 0x28]) + bytes(16))
    avc1 = _boite(b"avc1", bytes(78) + avcc)
    stsd = _boite(b"stsd", bytes(8) + avc1)
    stbl = _boite(b"stbl", stsd)
    minf = _boite(b"minf", stbl)
    mdia = _boite(b"mdia", minf)
    trak = _boite(b"trak", mdia)
    moov = _boite(b"moov", trak)
    ftyp = _boite(b"ftyp", b"isom" + bytes(4))
    return ftyp + moov


def _fragment(indice: int, taille_mdat: int = 400) -> bytes:
    moof = _boite(b"moof", bytes([indice]) * 16)
    mdat = _boite(b"mdat", bytes([0x10 + indice]) * (taille_mdat - 8))
    return moof + mdat


class FauxPipeMorceaux:
    def __init__(self, morceaux: list):
        self._morceaux = list(morceaux)

    def read(self, _n: int) -> bytes:
        return self._morceaux.pop(0) if self._morceaux else b""


class FauxDrapeauEnregistrement:
    """Remplace ENREGISTREMENT_DIRECT_ACTIF (un threading.Event) pour
    contrôler PRÉCISÉMENT à quel appel de _ecrire() l'enregistrement devient
    désiré : ``is_set()`` y est appelée exactement une fois par bloc traité,
    ce qui donne un point de synchronisation fiable - contrairement à un
    déclencheur posé sur la lecture du pipe, qui tourne dans le thread
    séparé de LecteurTube, à sa propre vitesse, sans rapport avec le rythme
    de consommation de _ecrire() (constaté en écrivant ce test : le thread
    de lecture vide tout le pipe simulé quasi instantanément)."""

    def __init__(self, actif_a_partir_de: int):
        self._appels = 0
        self._actif_a_partir_de = actif_a_partir_de

    def is_set(self) -> bool:
        self._appels += 1
        return self._appels >= self._actif_a_partir_de

    def clear(self):
        pass


class FauxVerrou:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FauxProcessus:
    def __init__(self, pipe):
        self.stdout = pipe
        self.stderr = io.BytesIO(b"")

    def terminate(self):
        pass

    def wait(self, timeout=None):
        pass

    def kill(self):
        pass


def _parser_boites_top_level(donnees: bytes):
    boites, position = [], 0
    while position + 8 <= len(donnees):
        taille = int.from_bytes(donnees[position:position + 4], "big")
        genre = donnees[position + 4:position + 8]
        if taille < 8 or position + taille > len(donnees):
            break
        boites.append((genre, position, taille))
        position += taille
    return boites, position == len(donnees)


class TestsEnregistrementDemarreEnCoursDeDirect(unittest.TestCase):
    def setUp(self):
        serve.MODULE_SLOT_INFO.clear()
        serve._effacer_erreur_direct()
        self.addCleanup(serve.MODULE_SLOT_INFO.clear)
        self.addCleanup(serve._effacer_erreur_direct)
        # DOSSIER_DIRECT est figé au premier import de `serve` dans le
        # processus (BLINK_HOME n'a d'effet que sur ce tout premier import) :
        # un autre fichier de test chargé avant celui-ci dans la même suite
        # peut avoir déjà fixé un autre dossier. Mocker la variable
        # directement isole ce test de cet ordre de chargement.
        self._dossier_direct = tempfile.TemporaryDirectory(prefix="blink-direct-")
        self.addCleanup(self._dossier_direct.cleanup)
        self._patch_dossier = mock.patch.object(
            serve, "DOSSIER_DIRECT", Path(self._dossier_direct.name)
        )
        self._patch_dossier.start()
        self.addCleanup(self._patch_dossier.stop)

    @staticmethod
    def _handler():
        handler = serve.Handler.__new__(serve.Handler)
        handler.ffmpeg = "ffmpeg-test"
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.send_error = mock.Mock()
        return handler

    def _executer(self, morceaux: list, actif_a_partir_de: int):
        pipe = FauxPipeMorceaux(morceaux + [b""])
        process = FauxProcessus(pipe)
        handler = self._handler()
        drapeau = FauxDrapeauEnregistrement(actif_a_partir_de)

        with mock.patch.object(serve, "MODULE_SLOT", threading.Semaphore(1)), \
             mock.patch.object(serve.blink_engine, "hub_lock", return_value=FauxVerrou()), \
             mock.patch.object(serve.BLINK, "call", return_value="rtsp://camera"), \
             mock.patch.object(serve.runtime, "demarrer", return_value=process), \
             mock.patch.object(serve, "ENREGISTREMENT_DIRECT_ACTIF", drapeau):
            serve.Handler.send_live_mse(handler, "Jardin")

        return list(serve.DOSSIER_DIRECT.rglob("*.mp4"))

    def test_demarrage_en_plein_milieu_d_un_mdat_attend_le_fragment_suivant(self):
        segment = _segment_synthetique()
        fragment1 = _fragment(1)
        fragment2 = _fragment(2)
        fragment3 = _fragment(3)

        # read_mp4_init_segment() s'arrête dès qu'un avcC est trouvé : ce
        # premier bloc, lui, ne contient QUE segment, pour que `first` soit
        # exactement le segment d'initialisation - pas un cas à part, mais
        # pour isoler clairement où tombe la coupure qu'on veut tester.
        # Deuxième bloc : fragment 1 complet, puis la moitié du mdat du
        # fragment 2 - la forme réelle constatée avec un vrai flux ffmpeg
        # (un mdat de plusieurs Ko scindé par un bloc de 16 Ko, voir le fix).
        milieu_mdat2 = len(fragment2) - 30
        second_bloc = fragment1 + fragment2[:milieu_mdat2]
        # Troisième bloc : la fin du mdat du fragment 2, SANS son moof (déjà
        # dans le bloc précédent) - c'est exactement le bloc sur lequel
        # l'ancien code démarrait un enregistrement à tort. C'est aussi
        # l'appel (le 3e, _ecrire(first) compté) où le drapeau bascule à
        # actif : l'enregistrement est désiré alors qu'on est en plein
        # milieu d'un mdat déjà entamé.
        fichiers = self._executer(
            [segment, second_bloc, fragment2[milieu_mdat2:], fragment3],
            actif_a_partir_de=3,
        )

        self.assertEqual(len(fichiers), 1, f"un seul enregistrement attendu, trouvé {fichiers}")
        contenu = fichiers[0].read_bytes()

        # Jamais le bloc coupé : le fichier ne doit contenir aucune trace du
        # mdat tronqué du fragment 2 sans son moof.
        self.assertNotIn(fragment2[milieu_mdat2:], contenu)
        # Démarre bien sur le fragment 3, qui suit immédiatement le segment
        # d'initialisation dans le fichier écrit.
        self.assertEqual(contenu, segment + fragment3)

        boites, complet = _parser_boites_top_level(contenu)
        self.assertTrue(complet, "chaque boîte du fichier doit être entière, sans reste")
        genres = [genre for genre, _, _ in boites]
        self.assertEqual(genres, [b"ftyp", b"moov", b"moof", b"mdat"])

    def test_demarrage_pile_sur_un_fragment_ecrit_normalement(self):
        """Non-régression : quand l'activation tombe déjà sur un début de
        fragment propre, l'enregistrement démarre dès ce bloc (pas de délai
        artificiel introduit par le correctif)."""
        segment = _segment_synthetique()
        fragment1 = _fragment(1)
        fragment2 = _fragment(2)

        # _ecrire(first=segment) est le 1er appel (drapeau encore inactif) ;
        # _ecrire(fragment1) est le 2e, où le drapeau bascule à actif -
        # fragment1 démarre pile par un moof, donc rien à attendre.
        fichiers = self._executer([segment, fragment1, fragment2], actif_a_partir_de=2)

        self.assertEqual(len(fichiers), 1)
        self.assertEqual(fichiers[0].read_bytes(), segment + fragment1 + fragment2)

    def test_demarrage_des_le_premier_bloc_est_inchange(self):
        """Non-régression : enregistrement déjà actif avant même le début du
        direct - le cas déjà correct avant ce correctif."""
        segment = _segment_synthetique()
        fragment1 = _fragment(1)

        fichiers = self._executer([segment, fragment1], actif_a_partir_de=1)

        self.assertEqual(len(fichiers), 1)
        self.assertEqual(fichiers[0].read_bytes(), segment + fragment1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
