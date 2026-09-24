"""Non-régression : md.save_json() appelé en parallèle sur un même fichier.

serve.py tourne sur un ThreadingHTTPServer, et la page charge /api/clips et
/api/videos en même temps (Promise.all dans load(), serve_app.js). Dès qu'il
existe au moins un enregistrement du direct et une vidéo assemblée, les deux
routes réécrivent ASSEMBLED_DURATIONS. Avec un temporaire au nom fixe
(``path.with_suffix(".tmp")``), le second ``replace`` trouvait le fichier déjà
consommé par le premier : FileNotFoundError, donc une requête en échec au
chargement de la page (reproduit 197 fois sur 600 écritures, audit du
2026-09-24)."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("BLINK_BOOTSTRAP", "none")

import merge_daily as md  # noqa: E402


class TestsSaveJsonConcurrent(unittest.TestCase):
    def setUp(self):
        dossier = tempfile.TemporaryDirectory(prefix="blink-save-json-")
        self.addCleanup(dossier.cleanup)
        self.dossier = Path(dossier.name)
        self.cible = self.dossier / "assembled_durations.json"

    def test_ecritures_paralleles_sans_exception_ni_residu(self):
        valeur = {f"daily/cam/{i}.mp4": {"duration": 12.5, "empreinte": [123, 1.0]}
                  for i in range(300)}
        erreurs = []
        depart = threading.Barrier(4)

        def ecrire():
            depart.wait()
            for _ in range(150):
                try:
                    md.save_json(self.cible, valeur)
                except Exception as erreur:  # noqa: BLE001 - tout échec compte
                    erreurs.append(erreur)

        fils = [threading.Thread(target=ecrire) for _ in range(4)]
        for fil in fils:
            fil.start()
        for fil in fils:
            fil.join()

        self.assertEqual(erreurs, [])
        self.assertEqual(md.load_json(self.cible, None), valeur)
        # Aucun temporaire laissé derrière, quel que soit son nom.
        self.assertEqual(sorted(p.name for p in self.dossier.iterdir()),
                         ["assembled_durations.json"])

    def test_semantique_windows_simulee_sans_exception(self):
        """Sous Windows, os.replace échoue (« Access is denied ») si la même
        cible est remplacée au même instant par un autre écrivain : simulé
        ici pour éprouver ce cas sur tous les systèmes, pas seulement en CI
        Windows (162 échecs sur 600 écritures avant correction)."""
        original = Path.replace
        en_cours = set()
        garde = threading.Lock()

        def replace_facon_windows(source, cible):
            cle = os.fspath(cible)
            with garde:
                if cle in en_cours:
                    raise PermissionError(13, "Access is denied")
                en_cours.add(cle)
            try:
                time.sleep(0.001)
                return original(source, cible)
            finally:
                with garde:
                    en_cours.discard(cle)

        erreurs = []
        depart = threading.Barrier(4)

        def ecrire():
            depart.wait()
            for _ in range(50):
                try:
                    md.save_json(self.cible, {"a": 1})
                except Exception as erreur:  # noqa: BLE001 - tout échec compte
                    erreurs.append(erreur)

        with mock.patch.object(Path, "replace", replace_facon_windows):
            fils = [threading.Thread(target=ecrire) for _ in range(4)]
            for fil in fils:
                fil.start()
            for fil in fils:
                fil.join()
        self.assertEqual(erreurs, [])
        self.assertEqual(md.load_json(self.cible, None), {"a": 1})

    def test_refus_windows_transitoire_reessaye(self):
        # Sous Windows, un lecteur qui tient la cible ouverte fait échouer
        # os.replace en PermissionError le temps de sa lecture.
        refus = PermissionError(13, "Access is denied")
        with mock.patch.object(md.runtime, "_ecrire_texte_atomique",
                               side_effect=[refus, refus, None]) as ecrire, \
                mock.patch.object(md.time, "sleep") as dormir:
            md.save_json(self.cible, {"a": 1})
        self.assertEqual(ecrire.call_count, 3)
        self.assertEqual(dormir.call_count, 2)

    def test_refus_persistant_finit_par_remonter(self):
        with mock.patch.object(md.runtime, "_ecrire_texte_atomique",
                               side_effect=PermissionError(13, "Access is denied")) as ecrire, \
                mock.patch.object(md.time, "sleep"):
            with self.assertRaises(PermissionError):
                md.save_json(self.cible, {"a": 1})
        self.assertEqual(ecrire.call_count, 10)

    def test_cree_le_dossier_parent(self):
        cible = self.dossier / "sous" / "dossier" / "etat.json"
        md.save_json(cible, {"a": 1})
        self.assertEqual(md.load_json(cible, {}), {"a": 1})


class TestsLoadJsonConcurrent(unittest.TestCase):
    """load_json() rendait son défaut sur un refus Windows passager : toute
    lecture-modification-écriture réécrivait alors un contenu vidé (mesuré :
    4 lectures sur 70 969 sous forte concurrence, 2026-09-24)."""

    def setUp(self):
        dossier = tempfile.TemporaryDirectory(prefix="blink-load-json-")
        self.addCleanup(dossier.cleanup)
        self.cible = Path(dossier.name) / "direct_exclusion.json"
        md.save_json(self.cible, {"a": 1})

    def test_refus_windows_transitoire_en_lecture_reessaye(self):
        original = Path.read_text
        refus = iter([PermissionError(13, "Access is denied")] * 2)

        def lire(chemin, *args, **kwargs):
            erreur = next(refus, None)
            if erreur is not None:
                raise erreur
            return original(chemin, *args, **kwargs)

        with mock.patch.object(Path, "read_text", lire), \
                mock.patch.object(md.time, "sleep") as dormir:
            self.assertEqual(md.load_json(self.cible, {}), {"a": 1})
        self.assertEqual(dormir.call_count, 2)

    def test_lecture_pendant_un_remplacement_attend_au_lieu_de_rendre_le_defaut(self):
        """Sémantique Windows simulée partout : lire une cible en cours de
        remplacement échoue. Le remplacement dure 0,2 s et la lecture part
        exactement pendant : elle doit rendre la nouvelle valeur."""
        lire_original, remplacer_original = Path.read_text, Path.replace
        en_cours = threading.Event()

        def lire(chemin, *args, **kwargs):
            if en_cours.is_set():
                raise PermissionError(13, "Access is denied")
            return lire_original(chemin, *args, **kwargs)

        def remplacer(source, cible):
            en_cours.set()
            try:
                time.sleep(0.2)
                return remplacer_original(source, cible)
            finally:
                en_cours.clear()

        with mock.patch.object(Path, "read_text", lire), \
                mock.patch.object(Path, "replace", remplacer):
            ecrivain = threading.Thread(
                target=md.save_json, args=(self.cible, {"a": 2}))
            ecrivain.start()
            self.assertTrue(en_cours.wait(5))
            lu = md.load_json(self.cible, {})
            ecrivain.join()
        self.assertEqual(lu, {"a": 2})


if __name__ == "__main__":
    unittest.main()
