"""Teste directement le POST de suppression d'un clip USB Blink.

Ce script n'appelle pas ``LocalStorageMediaItem.delete_video``. Il construit
lui-même l'URL de suppression fournie par le manifeste et effectue un unique
POST authentifié. Sans ``--supprimer``, il reste strictement en lecture seule.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time

import runtime

runtime.bootstrap()

from aiohttp import ClientSession  # noqa: E402

import blink_auth  # noqa: E402
import blink_models  # noqa: E402


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", default="Terrasse1")
    parser.add_argument("--clip-id", type=int, help="identifiant USB exact à cibler")
    parser.add_argument("--supprimer", action="store_true")
    parser.add_argument(
        "--confirmer",
        metavar="TEXTE",
        help="doit valoir SUPPRIMER-<clip-id>",
    )
    return parser.parse_args()


def meme_camera(clip, camera: str) -> bool:
    return str(clip.name).casefold().strip() == camera.casefold().strip()


def decrire(nom_hub: str, clip) -> str:
    instant = blink_models.clip_datetime_utc(clip).astimezone()
    return (
        f"id={clip.id} | {instant.isoformat()} | caméra={clip.name!r} | "
        f"hub={nom_hub!r}"
    )


async def lire_camera(blink, camera: str) -> list[tuple[str, object, object]]:
    trouves = []
    for nom_hub, sync in blink.sync.items():
        owner = f"essai-usb-post-{os.getpid()}-{nom_hub}"
        try:
            with runtime.verrou("hub", owner, stale_after=600, attente=5):
                manifeste = await blink_models.read_local_manifest(sync)
        except runtime.BusyError:
            print(f"Hub {nom_hub!r} occupé, manifeste non lu.")
            continue
        for clip in manifeste:
            if meme_camera(clip, camera):
                trouves.append((nom_hub, sync, clip))
    trouves.sort(
        key=lambda item: blink_models.clip_datetime_utc(item[2]),
        reverse=True,
    )
    return trouves


async def executer(args: argparse.Namespace) -> int:
    async with ClientSession() as session:
        blink = await blink_auth.connect_saved(session)
        if blink is None:
            print("Session Blink absente ou invalide.")
            return 2

        clips = await lire_camera(blink, args.camera)
        print(f"Caméra {args.camera!r} : {len(clips)} clip(s) USB trouvé(s).")
        for nom_hub, _, clip in clips[:20]:
            print("  " + decrire(nom_hub, clip))
        if len(clips) > 20:
            print(f"  ... {len(clips) - 20} autre(s) clip(s)")

        if args.clip_id is None:
            if args.supprimer:
                print("Refus : --supprimer exige --clip-id.")
                return 2
            print("Lecture seule terminée : aucun clip supprimé.")
            return 0

        resultat = next(
            (item for item in clips if int(item[2].id) == args.clip_id),
            None,
        )
        if resultat is None:
            print(f"Refus : clip {args.clip_id} absent de la caméra {args.camera!r}.")
            return 2
        nom_hub, sync, cible = resultat
        print("Cible vérifiée : " + decrire(nom_hub, cible))

        if not args.supprimer:
            print("Lecture seule terminée : aucun clip supprimé.")
            return 0
        confirmation = f"SUPPRIMER-{args.clip_id}"
        if args.confirmer != confirmation:
            print(f"Refus : ajoutez --confirmer {confirmation}.")
            return 2

        owner = f"essai-usb-post-{os.getpid()}-{nom_hub}"
        with runtime.verrou("hub", owner, stale_after=600, attente=5):
            # Un nouveau manifeste garantit que son identifiant et l'URL du clip
            # sont encore valides au moment précis de la suppression.
            manifeste = await blink_models.read_local_manifest(sync)
            cible = next(
                (
                    clip
                    for clip in manifeste
                    if meme_camera(clip, args.camera) and int(clip.id) == args.clip_id
                ),
                None,
            )
            if cible is None:
                print("Refus : la cible a disparu du manifeste actualisé.")
                return 2

            url = blink.urls.base_url + cible.url().replace("request", "delete")
            # Appel direct volontaire : ni delete_video(), ni boucle de reprise.
            debut_post = time.monotonic()
            reponse = await blink.auth.query(
                url=url,
                headers=blink.auth.header,
                reqtype="post",
                json_resp=False,
            )
            duree_post = time.monotonic() - debut_post
            if reponse is None:
                print(f"Aucune réponse reçue du service Blink après {duree_post:.2f} s.")
                return 1
            statut = reponse.status
            await reponse.read()
            print(f"Réponse Blink au POST : HTTP {statut} en {duree_post:.2f} s")
            if statut != 200:
                print("La suppression n'est pas confirmée par le service.")
                return 1

            debut_catalogue = time.monotonic()
            verification = await blink_models.read_local_manifest(sync)
            duree_catalogue = time.monotonic() - debut_catalogue
            encore_present = any(int(clip.id) == args.clip_id for clip in verification)
            if encore_present:
                print(
                    "Échec du contrôle : le clip figure encore dans le manifeste USB "
                    f"après {duree_catalogue:.2f} s."
                )
                return 1

        print(
            "Suppression confirmée : le clip a disparu du manifeste USB "
            f"(catalogue actualisé en {duree_catalogue:.2f} s)."
        )
        return 0


def main() -> int:
    try:
        return asyncio.run(executer(arguments()))
    except (KeyboardInterrupt, EOFError):
        print("Opération annulée.")
        return 130
    except Exception as erreur:
        print(f"Échec : {type(erreur).__name__}: {erreur}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
