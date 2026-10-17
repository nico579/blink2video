"""Un registre momentanément inaccessible ne doit jamais passer pour vide.

Incident réel du 2026-10-17 : la passe USB a lu le registre pendant que la
boucle cloud le remplaçait (os.replace). Sous Windows, cette lecture échoue en
PermissionError, et load_download_state() la traitait comme un registre
corrompu, donc vide : six clips déjà acquis ont été retéléchargés sous leur
nouveau numéro de manifeste, puis annoncés comme nouveaux.

Les deux premiers tests font tourner une vraie concurrence, entre deux
processus, sur un vrai fichier : ils échouaient sous Windows avant le
correctif, et passent sans effort ailleurs, où un remplacement ne bloque jamais
un lecteur. Les suivants simulent le refus pour fixer le contrat sur tous les
systèmes."""
from __future__ import annotations  # Python 3.8 (build Windows 7)

import json
import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from nico579_commons import atomique

import blink_registre
import runtime

DUREE_S = 2.0


def _etat(nombre: int) -> dict:
    return {"version": 2, "clips": {
        f"v2:{indice:05d}": {
            "hub": "Maison",
            "camera": "jardin",
            "created_at": (f"2026-10-{1 + indice // 1440 % 28:02d}"
                           f"T{indice // 60 % 24:02d}:{indice % 60:02d}:00+00:00"),
            "path": f"jardin/2026-10/clip-{indice:05d}.mp4",
            "bytes": 1000 + indice,
            "source": "usb",
        }
        for indice in range(nombre)
    }}


def _remplacer_sans_relache(registre: str, contenu: str, pret, arret) -> None:
    """La boucle cloud, dans son propre processus : réécrit le registre d'un bloc."""
    cible = Path(registre)
    temporaire = cible.with_name("autre-boucle.tmp")
    pret.set()
    while not arret.is_set():
        temporaire.write_text(contenu, encoding="utf-8")
        try:
            os.replace(temporaire, cible)
        except PermissionError:
            pass  # refusé pendant une lecture : retenté au tour suivant


def _lire_par_moments(registre: str, pret, arret) -> None:
    """La page ou l'autre boucle : lisent le registre par moments, pas en continu."""
    cible = Path(registre)
    pret.set()
    while not arret.is_set():
        try:
            cible.read_bytes()
        except OSError:
            pass  # croisé un remplacement : sans objet pour ce test
        time.sleep(0.002)


class _Registre(unittest.TestCase):
    nombre = 3

    def setUp(self):
        dossier = tempfile.TemporaryDirectory()
        self.addCleanup(dossier.cleanup)
        self.sortie = Path(dossier.name)
        self.registre = self.sortie / blink_registre.STATE_FILENAME
        self.contenu = json.dumps(_etat(self.nombre), indent=2, ensure_ascii=False)
        self.registre.write_text(self.contenu, encoding="utf-8")
        # Le verrou « registre » de save_download_state() reste dans le bac à sable.
        verrous = mock.patch.object(runtime, "app_dir", return_value=self.sortie)
        verrous.start()
        self.addCleanup(verrous.stop)


class TestsConcurrenceReelle(_Registre):
    # Un petit registre se relit vite : autant de lectures de plus, donc
    # d'occasions de croiser un remplacement pendant DUREE_S.
    nombre = 200

    def _en_parallele(self, travail, args: tuple, action) -> None:
        """Répète ``action`` pendant DUREE_S, ``travail`` tournant dans un
        second processus. Deux fils d'un même processus, sérialisés par le
        GIL, ne se croisaient presque jamais : le test passait sur le code
        fautif (essai du 2026-10-17)."""
        contexte = multiprocessing.get_context("spawn")
        pret, arret = contexte.Event(), contexte.Event()
        processus = contexte.Process(target=travail, args=(*args, pret, arret))
        processus.start()
        try:
            self.assertTrue(pret.wait(60), "processus concurrent jamais démarré")
            fin = time.monotonic() + DUREE_S
            while time.monotonic() < fin:
                action()
        finally:
            arret.set()
            processus.join(timeout=10)
            if processus.is_alive():
                processus.terminate()
                processus.join(timeout=5)
            processus.close()

    def test_lecture_pendant_des_remplacements_ne_rend_jamais_un_registre_vide(self):
        tailles = []
        self._en_parallele(
            _remplacer_sans_relache, (str(self.registre), self.contenu),
            lambda: tailles.append(
                len(blink_registre.load_download_state(self.sortie)["clips"])))

        incompletes = [taille for taille in tailles if taille != self.nombre]
        self.assertEqual(incompletes, [],
                         f"{len(incompletes)} lectures incomplètes sur {len(tailles)}")

    def test_ecriture_pendant_des_lectures_aboutit(self):
        etat = blink_registre.load_download_state(self.sortie)
        self._en_parallele(
            _lire_par_moments, (str(self.registre),),
            lambda: blink_registre.save_download_state(self.sortie, etat))

        self.assertEqual(
            len(blink_registre.load_download_state(self.sortie)["clips"]), self.nombre)


class TestsRefusSimule(_Registre):
    def setUp(self):
        super().setUp()
        # Les nouvelles tentatives sans leurs pauses de 50 ms.
        pauses = mock.patch.object(atomique.time, "sleep")
        pauses.start()
        self.addCleanup(pauses.stop)

    def _refuser(self, combien: int):
        """Refuse ``combien`` lectures du registre (toutes si négatif)."""
        lire = Path.read_text
        restants = [combien]

        def lecture(chemin, *args, **kwargs):
            if chemin == self.registre and restants[0] != 0:
                restants[0] -= 1
                raise PermissionError(13, "Accès refusé", str(chemin))
            return lire(chemin, *args, **kwargs)

        return mock.patch.object(Path, "read_text", autospec=True, side_effect=lecture)

    def test_refus_passager_est_reessaye(self):
        with self._refuser(2):
            etat = blink_registre.load_download_state(self.sortie)
        self.assertEqual(len(etat["clips"]), self.nombre)

    def test_refus_persistant_leve_au_lieu_de_rendre_un_registre_vide(self):
        with self._refuser(-1), self.assertRaises(PermissionError):
            blink_registre.load_download_state(self.sortie)

    def test_registre_illisible_n_est_jamais_ecrase(self):
        # Avant le correctif, la relecture de fusion rendait un registre vide
        # et l'écriture ne gardait que les entrées de l'appelant : tout
        # l'historique, exclusions comprises, disparaissait.
        nouveau = {"version": 2, "clips": {
            "v2:nouveau": _etat(1)["clips"]["v2:00000"]}}
        avant = self.registre.read_bytes()

        with self._refuser(-1), self.assertRaises(PermissionError):
            blink_registre.save_download_state(self.sortie, nouveau)

        self.assertEqual(self.registre.read_bytes(), avant)


if __name__ == "__main__":
    unittest.main()
