"""Non-regression d'une course reelle (2026-09-03, signalee par
l'utilisateur : "j'ai fait redemarrer avec le systray, ca s'est arrete,
mais pas reparti") entre le clic sur Redemarrer/Arreter dans l'icone de
zone de notification et la sortie du processus.

redemarrer()/arreter() (tray.py) lancent nettoyer() - et pour redemarrer,
_relancer() ensuite - sur un thread demon separe (necessaire : ce callback
tourne sur le thread de la pompe de messages de l'icone, le geler
empecherait icon.stop() d'etre traite a temps). Mais icon.run() rendait la
main des que icon.stop() etait appele, sans jamais attendre ce thread
demon : blink_cli.py (l'appelant) pouvait alors terminer son propre
nettoyage (redondant mais rapide une fois les workers deja arretes par le
premier) et laisser le processus sortir - tuant net le thread demon avant
qu'il n'ait atteint _relancer() (un thread demon ne survit jamais a la fin
du thread principal). "Redemarrer" arretait alors tout sans jamais rien
relancer, exactement le symptome signale."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-tray-race-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import pystray  # noqa: E402
import tray  # noqa: E402


class FauxIcon:
    """Remplace pystray.Icon : aucun vrai backend graphique, run() simule
    un clic en appelant directement l'item de menu demande, comme le
    ferait un vrai clic utilisateur (item(icon), signature confirmee dans
    pystray/_base.py MenuItem.__call__)."""

    prochain_libelle_a_cliquer = None

    def __init__(self, name, icon_img, title, menu=None):
        self.menu = menu
        self.arrete = False

    def stop(self):
        self.arrete = True

    def update_menu(self):
        pass

    def run(self):
        items = list(self.menu)
        cible = next(i for i in items if FauxIcon.prochain_libelle_a_cliquer in i.text)
        cible(self)


class TrayRedemarrerRaceTests(unittest.TestCase):
    def _executer_et_cliquer(self, libelle: str, nettoyer):
        arret = __import__("threading").Event()
        FauxIcon.prochain_libelle_a_cliquer = libelle
        with mock.patch.object(pystray, "Icon", FauxIcon):
            tray.executer(8765, arret, nettoyer)

    def test_redemarrer_attend_relancer_avant_de_rendre_la_main(self) -> None:
        ordre = []

        def nettoyer_lent():
            time.sleep(0.3)
            ordre.append("nettoyer")

        def faux_relancer(sans_relance):
            ordre.append("relancer")

        with mock.patch.object(tray, "_relancer", side_effect=faux_relancer):
            self._executer_et_cliquer("Redémarrer", nettoyer_lent)

        # Le point du correctif : executer() ne doit rendre la main
        # qu'apres relancer(), jamais avant - sans quoi blink_cli.py
        # pourrait laisser sortir le processus (nettoyage redondant mais
        # rapide) et tuer le thread demon avant qu'il n'atteigne
        # _relancer(), qui ne s'executerait alors jamais.
        self.assertEqual(ordre, ["nettoyer", "relancer"],
                         "executer() a rendu la main avant la fin de la sequence "
                         "nettoyer()+_relancer() : la course d'origine est revenue")

    def test_arreter_attend_nettoyer_avant_de_rendre_la_main(self) -> None:
        ordre = []

        def nettoyer_lent():
            time.sleep(0.3)
            ordre.append("nettoyer")

        with mock.patch.object(tray, "_relancer") as faux_relancer:
            self._executer_et_cliquer("Arrêter", nettoyer_lent)

        self.assertEqual(ordre, ["nettoyer"])
        faux_relancer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
