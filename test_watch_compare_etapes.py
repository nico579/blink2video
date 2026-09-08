"""Caractérise l'ordre et les frontières des étapes de surveillance."""

import copy
import datetime as dt
import os
import tempfile
import unittest
from unittest import mock

_IMPORT_HOME = tempfile.TemporaryDirectory(prefix="blink-watch-compare-import-")
with mock.patch.dict(os.environ, {"BLINK_BOOTSTRAP": "none", "BLINK_HOME": _IMPORT_HOME.name}):
    import runtime
    import watch


MAINTENANT = dt.datetime(2026, 9, 8, 12, tzinfo=dt.timezone.utc)


class DateFixe(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return MAINTENANT.astimezone(tz) if tz else MAINTENANT.replace(tzinfo=None)


def camera(**changements):
    return {"online": True, "armed": True, "battery": "ok", "system_armed": True,
            **changements}


class TestsComparaisonEtapes(unittest.TestCase):
    def setUp(self):
        langue = mock.patch.object(runtime, "lire_langue", return_value="fr")
        self.langue = langue.start()
        self.addCleanup(langue.stop)
        horloge = mock.patch.object(watch.dt, "datetime", DateFixe)
        horloge.start()
        self.addCleanup(horloge.stop)

    @staticmethod
    def comparer(avant, courant, ignores=()):
        return watch.compare(avant, courant, dt.timezone.utc, set(ignores))

    def test_ordre_alertes_modules_cameras_systeme_puis_silence(self):
        avant = {"at": (MAINTENANT - dt.timedelta(hours=2)).isoformat(),
                 "cameras": {nom: camera() for nom in ("Zebra", "Alpha", "Delta", "Charlie")}}
        courant = {
            "modules": [{"name": "Z-module", "online": False},
                        {"name": "A-module", "online": False}],
            "cameras": {
                "Zebra": camera(online=False, armed=False, battery="low", system_armed=False),
                "Delta": camera(system_armed=False),
                "Alpha": camera(online=False, armed=False, battery="low", system_armed=False),
                "Charlie": camera(system_armed=False),
            },
            "last_clip": {nom: (MAINTENANT - dt.timedelta(days=2)).isoformat()
                          for nom in ("Delta", "Charlie")},
        }
        self.assertEqual(self.comparer(avant, courant), ([
            "Module « Z-module » hors ligne.",
            "Module « A-module » hors ligne.",
            "Caméra « Alpha » hors ligne.",
            "Caméra « Alpha » : batterie « low ».",
            "Caméra « Alpha » : détection coupée.",
            "Caméra « Zebra » hors ligne.",
            "Caméra « Zebra » : batterie « low ».",
            "Caméra « Zebra » : détection coupée.",
            "Système entièrement désarmé.",
            "Caméra « Delta » : aucun clip depuis 2 jour(s).",
            "Caméra « Charlie » : aucun clip depuis 2 jour(s).",
        ], []))

    def test_ordre_retours_modules_puis_cameras_sans_retour_batterie(self):
        avant = {"modules": [{"name": nom, "online": False} for nom in ("A", "Z")],
                 "cameras": {nom: camera(online=False, armed=False, battery="low")
                             for nom in ("Zebra", "Alpha")}}
        courant = {"modules": [{"name": nom, "online": True} for nom in ("Z", "A")],
                   "cameras": {nom: camera() for nom in ("Zebra", "Alpha")}}
        self.assertEqual(self.comparer(avant, courant), ([], [
            "Module « Z » de nouveau en ligne.",
            "Module « A » de nouveau en ligne.",
            "Caméra « Alpha » de nouveau en ligne.",
            "Caméra « Alpha » : détection réactivée.",
            "Caméra « Zebra » de nouveau en ligne.",
            "Caméra « Zebra » : détection réactivée.",
        ]))

    def test_premiere_observation_repetition_et_retour(self):
        incident = {"cameras": {"Salon": camera(online=False, armed=False, battery="low",
                                                system_armed=False)}}
        self.assertEqual(self.comparer({}, incident), ([
            "Caméra « Salon » hors ligne.",
            "Caméra « Salon » : batterie « low ».",
            "Caméra « Salon » : détection coupée.",
            "Système entièrement désarmé.",
        ], []))
        self.assertEqual(self.comparer(incident, incident), ([], []))
        self.assertEqual(self.comparer(incident, {"cameras": {"Salon": camera()}}), ([], [
            "Caméra « Salon » de nouveau en ligne.",
            "Caméra « Salon » : détection réactivée.",
        ]))

    def test_anglais_couvre_les_messages_des_quatre_etapes(self):
        self.langue.return_value = "en"
        courant = {
            "modules": [{"name": "Maison", "online": False}],
            "cameras": {
                "Salon": camera(online=False, armed=False, battery="low", system_armed=False),
                "Terrasse": camera(system_armed=False),
            },
            "last_clip": {"Terrasse": (MAINTENANT - dt.timedelta(days=3)).isoformat()},
        }
        self.assertEqual(self.comparer({}, courant), ([
            'Module "Maison" offline.',
            'Camera "Salon" offline.',
            'Camera "Salon": battery "low".',
            'Camera "Salon": detection disabled.',
            "System fully disarmed.",
            'Camera "Terrasse": no clip for 3 day(s).',
        ], []))
        retabli = {"modules": [{"name": "Maison", "online": True}],
                   "cameras": {"Salon": camera(), "Terrasse": camera()}}
        self.assertEqual(self.comparer(courant, retabli), ([], [
            'Module "Maison" back online.',
            'Camera "Salon" back online.',
            'Camera "Salon": detection re-enabled.',
        ]))

    def test_sourdine_supprime_alertes_et_retours_camera_pas_les_modules(self):
        avant = {"modules": [{"name": "Maison", "online": False}],
                 "cameras": {"Salon": camera(online=False, armed=False)}}
        courant = {"modules": [{"name": "Maison", "online": True},
                               {"name": "Annexe", "online": False}],
                   "cameras": {"Salon": camera(battery="low", system_armed=False)},
                   "last_clip": {"Salon": (MAINTENANT - dt.timedelta(days=4)).isoformat()}}
        self.assertEqual(self.comparer(avant, courant, {"Salon"}), (
            ["Module « Annexe » hors ligne."], ["Module « Maison » de nouveau en ligne."]))

    def test_sourdine_ne_rend_pas_unique_un_ancien_nom_ambigu(self):
        avant = {"cameras": {"Jardin": camera(online=False, battery="low")}}
        courant = {"cameras": {
            "Jardin maison": camera(name="Jardin", network_id="1", device_id="10",
                                     online=False, battery="low"),
            "Jardin annexe": camera(name="Jardin", network_id="2", device_id="20"),
        }}
        self.assertEqual(self.comparer(avant, courant, {"Jardin annexe"}), ([
            "Caméra « Jardin maison » hors ligne.",
            "Caméra « Jardin maison » : batterie « low ».",
        ], []))

    def test_identite_suit_renommage_sans_emprunter_etat_autre_reseau(self):
        avant = {"cameras": {
            "Autre module": camera(name="Jardin", network_id="2", device_id="10",
                                    online=False, armed=False),
            "Ancien libellé": camera(name="Jardin", network_id="1", device_id="10"),
        }}
        courant = {"cameras": {
            "Nouveau libellé": camera(name="Terrasse", network_id="1", device_id="10"),
        }}
        self.assertEqual(self.comparer(avant, courant), ([], []))

    def test_ancien_nom_unique_est_repris_sans_identite_mais_pas_avec_autre_identite(self):
        courant = {"cameras": {"Jardin": camera(name="Jardin", network_id="1", device_id="10",
                                                online=False, battery="low")}}
        historique = camera(online=False, battery="low")
        self.assertEqual(self.comparer({"cameras": {"Jardin": historique}}, courant), ([], []))
        autre = {**historique, "name": "Jardin", "network_id": "2", "device_id": "10"}
        self.assertEqual(self.comparer({"cameras": {"Jardin": autre}}, courant), ([
            "Caméra « Jardin » hors ligne.", "Caméra « Jardin » : batterie « low ».",
        ], []))

    def test_sourdine_suit_une_identite_renommee(self):
        avant = {"cameras": {
            "Ancien": camera(name="Jardin", network_id="1", device_id="10"),
        }}
        courant = {"cameras": {
            "Nouveau": camera(name="Terrasse", network_id="1", device_id="10",
                              online=False, armed=False, battery="low", system_armed=False),
        }}
        self.assertEqual(self.comparer(avant, courant, {"Ancien"}), ([], []))

    def test_seuil_silence_utilise_les_jours_complets(self):
        seuil = dt.timedelta(days=watch.SILENCE_DAYS)
        for age, attendu in ((seuil - dt.timedelta(seconds=1), []),
                              (seuil, [f"Caméra « Jardin » : aucun clip depuis {watch.SILENCE_DAYS} jour(s)."]),
                              (seuil + dt.timedelta(hours=23),
                               [f"Caméra « Jardin » : aucun clip depuis {watch.SILENCE_DAYS} jour(s)."]),
                              (-dt.timedelta(seconds=1), [])):
            with self.subTest(age=age):
                courant = {"cameras": {"Jardin": camera()},
                           "last_clip": {"Jardin": (MAINTENANT - age).isoformat()}}
                self.assertEqual(self.comparer({}, courant), (attendu, []))

    def test_silence_alerte_au_franchissement_pas_apres_seuil_deja_franchi(self):
        dernier = MAINTENANT - dt.timedelta(days=watch.SILENCE_DAYS, hours=1)
        seuil = dernier + dt.timedelta(days=watch.SILENCE_DAYS)
        courant = {"cameras": {"Jardin": camera()}, "last_clip": {"Jardin": dernier.isoformat()}}
        for passage, attendu in ((seuil - dt.timedelta(seconds=1),
                                  [f"Caméra « Jardin » : aucun clip depuis {watch.SILENCE_DAYS} jour(s)."]),
                                 (seuil, []), (MAINTENANT, [])):
            with self.subTest(passage=passage):
                self.assertEqual(self.comparer({"at": passage.isoformat()}, courant), (attendu, []))

    def test_silence_ancien_passage_absent_vide_ou_invalide_permet_premiere_alerte(self):
        courant = {"cameras": {"Jardin": camera()},
                   "last_clip": {"Jardin": (MAINTENANT - dt.timedelta(days=5)).isoformat()}}
        for avant in ({}, {"at": None}, {"at": ""}, {"at": "date incorrecte"}):
            with self.subTest(avant=avant):
                self.assertEqual(self.comparer(avant, courant), (
                    ["Caméra « Jardin » : aucun clip depuis 5 jour(s)."], []))

    def test_silence_ignore_absentes_sourdines_hors_ligne_desarmees_et_dates_invalides(self):
        vieux = (MAINTENANT - dt.timedelta(days=5)).isoformat()
        cameras = {"Sourdine": camera(), "Hors ligne": camera(online=False),
                   "Désarmée": camera(armed=False), "Date invalide": camera(),
                   "Sans clip": camera()}
        courant = {"cameras": cameras,
                   "last_clip": {"Absente": vieux, "Sourdine": vieux, "Hors ligne": vieux,
                                 "Désarmée": vieux, "Date invalide": "date incorrecte"}}
        self.assertEqual(self.comparer({"cameras": cameras}, courant, {"Sourdine"}), ([], []))

    def test_systeme_desarme_consulte_aussi_ancien_etat_des_cameras_en_sourdine(self):
        avant = {"cameras": {"Visible": camera(system_armed=False), "Muette": camera()}}
        courant = {"cameras": {"Visible": camera(system_armed=False), "Muette": camera()}}
        self.assertEqual(self.comparer(avant, courant, {"Muette"}), (
            ["Système entièrement désarmé."], []))

    def test_comparaison_ne_modifie_pas_les_etats_ni_la_sourdine(self):
        avant = {"cameras": {"Ancien": camera(name="Jardin", network_id="1", device_id="10")}}
        courant = {"modules": [{"name": "Maison", "online": False}],
                   "cameras": {"Nouveau": camera(name="Terrasse", network_id="1", device_id="10")}}
        ignores = {"Ancien"}
        copie = copy.deepcopy((avant, courant, ignores))
        watch.compare(avant, courant, dt.timezone.utc, ignores)
        self.assertEqual((avant, courant, ignores), copie)


if __name__ == "__main__":
    unittest.main()
