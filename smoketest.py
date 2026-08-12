"""Vérifie qu'une installation fonctionne réellement, chez l'utilisateur.

Distinct de tests.py, qui éprouve le code sur un service d'intégration. Celui-ci
répond à une autre question : « est-ce que ça marche sur *cette* machine ? ».
Il s'adresse à quelqu'un qui vient d'installer l'outil et veut le savoir avant
de compter dessus.

Il produit donc une vraie vidéo horodatée qu'on peut ouvrir et regarder, fait
apparaître une vraie notification, et dit ce qu'il en est de la session Blink et
du démarrage automatique. Il ne touche ni à vos clips, ni à vos vidéos : tout se
passe dans un dossier temporaire, sauf la vidéo de démonstration, laissée à
l'endroit indiqué pour que vous puissiez la regarder.

    blink2video smoketest
    blink2video smoketest --keep    conserve le dossier de travail
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import runtime

runtime.bootstrap()

import merge_daily as md


CONSTATS = []


def constat(ok: bool, intitule: str, detail: str = "") -> bool:
    marque = "ok  " if ok else "ECHEC"
    print(f"  {marque} {intitule}" + (f"\n        {detail}" if detail else ""))
    CONSTATS.append(ok)
    return ok


def pixels_allumes(ffmpeg: str, video: Path) -> int:
    """Compte les pixels clairs dans le bas de l'image, où s'écrit l'heure."""
    resultat = runtime.lancer(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "1",
         "-i", str(video), "-frames:v", "1",
         "-vf", "crop=iw:ih/6:0:ih*5/6,format=gray", "-f", "rawvideo", "-"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, check=False,
    )
    return sum(1 for octet in resultat.stdout if octet > 60)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true",
                        help="conserver le dossier de travail au lieu de l'effacer")
    parser.add_argument("--timezone", default="Europe/Paris")
    args = parser.parse_args()

    print("Contrôle de l'installation\n")

    print("Vidéo")
    try:
        ffmpeg = md.find_ffmpeg()
        constat(True, "ffmpeg trouvé", ffmpeg)
    except RuntimeError as erreur:
        constat(False, "ffmpeg trouvé", str(erreur))
        return bilan()

    constat(md.has_drawtext(ffmpeg), "ffmpeg sait incruster du texte",
            "" if md.has_drawtext(ffmpeg)
            else "sans cela les vidéos sortiraient sans horodatage")

    try:
        police = md.find_font(None)
        constat(True, "police trouvée", str(police))
    except RuntimeError as erreur:
        constat(False, "police trouvée", str(erreur))
        return bilan()

    travail = Path(tempfile.mkdtemp(prefix="blink_smoketest_"))
    demonstration = runtime.app_dir() / "smoketest.mp4"
    try:
        # Un clip noir : tout pixel allumé dans le bas de l'image ne pourra
        # venir que de l'horodatage, ce qui rend la preuve indiscutable.
        source = travail / "source.mp4"
        runtime.lancer(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "color=c=black:s=1280x720:d=4:r=30", "-c:v", "libx264",
             "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(source)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, check=True,
        )
        constat(pixels_allumes(ffmpeg, source) == 0,
                "clip d'essai fabriqué, entièrement noir")

        import datetime as dt
        from zoneinfo import ZoneInfo

        fuseau = ZoneInfo(args.timezone)
        maintenant = dt.datetime.now(dt.timezone.utc)
        clip = md.ClipInfo(created=maintenant, source=source, duration=4.0,
                           width=1280, height=720, fps=30.0, has_audio=False)
        ok, erreur = md.run_ffmpeg_batch(
            ffmpeg, [clip], 1280, 720, 30.0, fuseau,
            md.quote_filter_path(police), "veryfast", 23, demonstration,
        )
        constat(ok, "vidéo horodatée produite", erreur)
        if ok:
            allumes = pixels_allumes(ffmpeg, demonstration)
            constat(allumes > 200, "l'heure est réellement dessinée dans l'image",
                    f"{allumes} pixels allumés dans la zone du cartouche")
            print(f"        à regarder : {demonstration}")
    finally:
        if args.keep:
            print(f"        dossier de travail conservé : {travail}")
        else:
            shutil.rmtree(travail, ignore_errors=True)

    print("\nNotification")
    try:
        import watch

        watch.toast("blink2video", "Contrôle d'installation : ceci est un essai.",
                    url="http://127.0.0.1:8765/")
        constat(True, "notification envoyée",
                "elle doit apparaître à l'écran ; sinon, voir les limites du README")
    except Exception as erreur:
        constat(False, "notification envoyée", f"{type(erreur).__name__}: {erreur}")

    print("\nCompte Blink")
    session = runtime.app_dir() / "blink_auth.json"
    if not session.is_file():
        constat(False, "session enregistrée",
                "lancez « blink2video login » ; sans elle, ni téléchargement ni direct")
    else:
        constat(True, "session enregistrée", str(session))
        registre = md.load_json(runtime.app_dir() / "Blink_Clips" / md.DOWNLOAD_STATE, {})
        clips = registre.get("clips") or {}
        ecartes = sum(1 for c in clips.values() if isinstance(c, dict) and c.get("excluded"))
        constat(bool(clips), "clips déjà récupérés",
                f"{len(clips)} clip(s) dont {ecartes} écarté(s)" if clips
                else "aucun ; lancez « blink2video download »")

    print("\nDémarrage automatique")
    try:
        import autostart

        autostart.appliquer("status")
    except Exception as erreur:
        print(f"  indéterminé : {erreur}")

    return bilan()


def bilan() -> int:
    echecs = CONSTATS.count(False)
    print()
    if echecs:
        print(f"{echecs} point(s) à regarder.")
        return 1
    print("Installation opérationnelle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
