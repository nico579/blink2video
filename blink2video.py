"""Point d'entrée : dispatch des verbes, rien d'autre.

Depuis l'étape 8 du plan de remédiation (AUDIT-2026-08-13.md, section 20),
la logique métier vit dans cinq modules séparés :

- blink_auth.py     : session Blink, identifiants, 2FA (8.1) ;
- blink_models.py   : clips USB/cloud, identité, chemins (8.2) ;
- blink_registre.py : registre incrémental, corrélation, réparation (8.3) ;
- blink_engine.py   : un passage de téléchargement, en boucle ou une fois (8.4) ;
- blink_cli.py       : grammaire, verbes délégués, supervision (8.5).

Ce fichier ne fait plus que router (8.6). `runtime.bootstrap()` n'est plus
appelé ici : blink_cli.py le fait, juste avant d'importer les modules qui ont
réellement besoin d'aiohttp ou de blinkpy (main, la branche « start » de
executer, accueillir) — jamais pour --help, --version, stop, open ou update
(O-06/8.7/8.8)."""

import sys

# asyncio/datetime/time ne servent à rien ici : ce sont des modules stdlib
# partagés (Python les met en cache une seule fois), gardés importés pour que
# les tests qui patchent `blink2video.time`/`.asyncio`/`.dt` continuent
# d'atteindre le même objet que blink_engine.py/blink_cli.py, sans connaître
# ces modules internes.
import asyncio  # noqa: F401
import datetime as dt  # noqa: F401
import time  # noqa: F401

import runtime

if __name__ == "__main__":
    # Avant blink_cli : ses modules tirent des constantes du dossier d'état
    # et de celui des sorties dès leur import. Reprend, une fois, l'état
    # d'une version ≤ 0.13. Au lancement seulement : un test qui importe ce
    # module ne doit jamais toucher au vrai dossier d'état.
    runtime.preparer_etat()

from blink_cli import msg, route  # noqa: E402


if __name__ == "__main__":
    try:
        raise SystemExit(route(sys.argv[1:]))
    except ValueError as erreur:
        print(f"{erreur}. " + msg("liste_verbes", liste=", ".join(runtime.VERBES)))
        raise SystemExit(2)
    except (KeyboardInterrupt, EOFError):
        print(msg("connexion_annulee"))
        raise SystemExit(130)
