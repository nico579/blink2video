"""Répète des verbes à intervalle régulier, en nommant lesquels.

La boucle était auparavant enfouie dans « watch », qui faisait donc quatre
choses que son nom ne disait pas : surveiller, télécharger, assembler et servir
l'interface. Trois interrupteurs permettaient d'en retirer, ce qui est le signe
d'un verbe trop chargé : on décrit alors ce qu'il ne fait pas.

Ici la répétition est le verbe, et ce qu'elle répète s'énumère :

    blink loop                          watch download merge, le défaut
    blink loop watch                    seulement les alertes
    blink loop download merge           rapatrier et assembler, sans alerte
    blink loop --interval 30            un tour toutes les demi-heures

L'interface web n'est pas un verbe répété : c'est un service, levé une fois
avant la première itération, d'où une option et non un verbe. Un seul lancement
suffit donc à tout mettre en place :

    blink loop --serve                  interface, puis la boucle

L'ordre d'un tour ne suit pas celui de la ligne de commande, mais celui des
dépendances : on télécharge avant d'assembler, et on contrôle l'état d'abord,
pour qu'une panne soit signalée même si le téléchargement échoue ensuite.
"""

import argparse
import sys
import time

import runtime

runtime.bootstrap()

import watch


# Ce que la boucle répète quand on ne lui dit rien : l'usage courant.
DEFAUT = ("watch", "download", "merge")

# L'ordre de ce tableau est celui d'exécution, indépendamment de l'ordre de la
# ligne de commande : on télécharge avant d'assembler.
ACTIVITES = DEFAUT


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="loop",
        description=__doc__.splitlines()[0],
        epilog="Verbes répétables : " + ", ".join(ACTIVITES)
               + ". L'interface se lance à part : blink serve.",
    )
    parser.add_argument(
        "verbes", nargs="*", metavar="VERBE",
        help="ce qu'il faut répéter (défaut : " + " ".join(DEFAUT) + ")",
    )
    parser.add_argument(
        "--interval", type=int, default=10, metavar="MINUTES",
        help="délai entre deux tours (défaut : 10)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="un seul tour, puis s'arrêter",
    )
    parser.add_argument(
        "--serve", action="store_true",
        help="lever l'interface web une fois avant de boucler. Ce n'est pas un "
             "verbe répété mais un service, d'où une option",
    )
    parser.add_argument(
        "--port", type=int, default=8765,
        help="port de l'interface (défaut : 8765)",
    )
    parser.add_argument("--timezone", default="Europe/Paris")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="montrer les alertes sans notifier, sans enregistrer l'état ni "
             "rien télécharger",
    )
    parser.add_argument(
        "--notify", choices=("popup", "mail", "both"), default="popup",
        help="comment signaler une anomalie (défaut : une boîte de dialogue)",
    )
    return parser.parse_args()


def main() -> int:
    from zoneinfo import ZoneInfo

    args = parse_args()
    demandes = tuple(args.verbes) or DEFAUT
    inconnus = [v for v in demandes if v not in ACTIVITES]
    if inconnus:
        print("Verbe non répétable : " + ", ".join(inconnus))
        print("Attendus : " + ", ".join(ACTIVITES))
        if "serve" in inconnus:
            print("L'interface est un service, pas une tâche : "
                  "« blink serve », ou « blink autostart on serve ».")
        return 1

    config = watch.load_config()
    timezone = ZoneInfo(args.timezone)
    a_faire = [v for v in ACTIVITES if v in demandes]

    if args.serve:
        if watch.ensure_server(args.port):
            print(f"Interface levée sur http://127.0.0.1:{args.port}/")
        else:
            print(f"Interface déjà en fonctionnement sur le port {args.port}.")

    print(f"Boucle sur {', '.join(a_faire)}, un tour toutes les "
          f"{args.interval} min. Ctrl+C pour arrêter.")
    watch.journal(f"boucle sur {' '.join(a_faire)} ({args.interval} min)")
    try:
        while True:
            watch.un_tour(args, config, timezone, a_faire)
            if args.once:
                return 0
            time.sleep(args.interval * 60)
    except KeyboardInterrupt:
        watch.journal("arret de la boucle")
        print("\nArrêt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
