"""Vérifie la chaîne vidéo et l'interface, sans compte Blink ni matériel.

L'astuce qui rend ces tests possibles partout : ffmpeg sait fabriquer des clips.
On génère donc une installation fictive, avec des clips de durées, de
définitions et de bandes-son différentes, on écrit le registre de
téléchargement que blink.py aurait écrit, et on déroule tout le reste. Ce qui
dépend du compte Amazon (connexion, téléchargement, direct, armement) n'est pas
testable ici et ne doit pas l'être : des identifiants n'ont rien à faire dans un
service d'intégration.

    python tests.py

Aucun effet sur vos données : tout se passe dans un dossier temporaire désigné
par BLINK_HOME.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import merge_daily as md


BASE_DIR = Path(__file__).resolve().parent
ECHECS = []


def verifier(condition: bool, intitule: str, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'ECHEC'} {intitule}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        ECHECS.append(intitule)


def fabriquer_clip(ffmpeg: str, cible: Path, secondes: float,
                   largeur: int, hauteur: int, muet: bool) -> None:
    """Produit un clip de synthèse : mire animée, et son sauf si muet.

    Les clips muets comptent : une caméra qui n'enregistre pas de son a bien
    failli casser l'assemblage, une piste silencieuse devant être fabriquée
    pour chacun."""
    cible.parent.mkdir(parents=True, exist_ok=True)
    commande = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i",
                f"testsrc=size={largeur}x{hauteur}:rate=30:duration={secondes}"]
    if not muet:
        commande += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={secondes}"]
    commande += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if not muet:
        commande += ["-c:a", "aac", "-shortest"]
    commande += [str(cible)]
    subprocess.run(commande, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, check=True)


def installation_fictive(racine: Path, ffmpeg: str) -> dict:
    """Fabrique des clips et le registre que le téléchargeur aurait écrit."""
    clips = [
        # (jour, heure UTC, durée, largeur, hauteur, muet)
        ("2026-03-01", "08-00-00", 4.0, 640, 360, False),
        ("2026-03-01", "09-30-00", 2.0, 640, 360, True),
        ("2026-03-02", "10-15-00", 3.0, 640, 360, True),
        ("2026-03-02", "11-45-00", 5.0, 640, 360, False),
    ]
    registre = {"version": 1, "clips": {}}
    for index, (jour, heure, duree, largeur, hauteur, muet) in enumerate(clips):
        relatif = f"jardin/2026-03/{jour}_{heure}Z_jardin_{1000 + index}.mp4"
        fabriquer_clip(ffmpeg, racine / "Blink_Clips" / relatif,
                       duree, largeur, hauteur, muet)
        registre["clips"][f"1:jardin:{jour}T{heure.replace('-', ':')}+00:00"] = {
            "hub": "Test", "camera": "jardin",
            "created_at": f"{jour}T{heure.replace('-', ':')}+00:00",
            "path": relatif, "bytes": 1000,
        }
    md.save_json(racine / "Blink_Clips" / md.DOWNLOAD_STATE, registre)
    return {"attendus": len(clips), "duree_totale": sum(c[2] for c in clips)}


def lancer(racine: Path, arguments: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-u", str(BASE_DIR / "merge_daily.py"), *arguments],
        cwd=str(BASE_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, BLINK_HOME=str(racine), PYTHONIOENCODING="utf-8"),
        check=False,
    )


def duree(ffmpeg: str, fichier: Path) -> float:
    return md.probe_clip_info(ffmpeg, fichier)[0]


def main() -> int:
    ffmpeg = md.find_ffmpeg()
    print(f"ffmpeg : {ffmpeg}")

    racine = Path(tempfile.mkdtemp(prefix="blink_tests_"))
    try:
        attendu = installation_fictive(racine, ffmpeg)

        print("\nAssemblage initial")
        sortie = lancer(racine, ["--timezone", "UTC"])
        verifier(sortie.returncode == 0, "l'assemblage se termine sans erreur",
                 sortie.stdout[-400:])

        normalises = list((racine / "Blink_Normalized").rglob("*.mp4"))
        verifier(len(normalises) == attendu["attendus"],
                 f"{attendu['attendus']} clips normalisés", str(len(normalises)))

        journalieres = sorted((racine / "Blink_Daily" / "jardin").glob("*.mp4"))
        verifier(len(journalieres) == 2, "deux journalières", str(len(journalieres)))

        total = sum(duree(ffmpeg, f) for f in journalieres)
        verifier(abs(total - attendu["duree_totale"]) < 0.5,
                 "aucune seconde perdue à l'assemblage",
                 f"{total:.2f} au lieu de {attendu['duree_totale']:.2f}")

        mensuelle = racine / "Blink_Monthly" / "jardin" / "2026-03_jardin.mp4"
        verifier(mensuelle.is_file(), "la mensuelle est produite")
        if mensuelle.is_file():
            verifier(abs(duree(ffmpeg, mensuelle) - attendu["duree_totale"]) < 0.5,
                     "la mensuelle couvre toute la période",
                     f"{duree(ffmpeg, mensuelle):.2f}")

        print("\nSecond passage : rien ne doit être refait")
        sortie = lancer(racine, ["--timezone", "UTC"])
        verifier("0 clip(s) encodé(s)" in sortie.stdout,
                 "aucun ré-encodage inutile", sortie.stdout[-300:])
        verifier("0 créée(s)" in sortie.stdout, "aucun réassemblage inutile")

        print("\nExclusion d'un clip")
        premier = sorted((racine / "Blink_Clips").rglob("*.mp4"))[0]
        sortie = lancer(racine, ["--timezone", "UTC", "--exclude", str(premier)])
        verifier(sortie.returncode == 0, "l'exclusion se termine sans erreur",
                 sortie.stdout[-300:])
        verifier(not premier.exists(), "le brut quitte Blink_Clips")
        verifier((racine / "Blink_Excluded" / premier.name).exists()
                 or any(f.name == premier.name
                        for f in (racine / "Blink_Excluded").rglob("*.mp4")),
                 "le brut est conservé dans Blink_Excluded")
        nouvelle = sum(duree(ffmpeg, f)
                       for f in (racine / "Blink_Daily" / "jardin").glob("*.mp4"))
        verifier(nouvelle < total - 1,
                 "la durée totale diminue du clip écarté",
                 f"{nouvelle:.2f} contre {total:.2f}")

        print("\nInterface web")
        serveur = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "review.py"), "--no-browser", "--port", "8899"],
            cwd=str(BASE_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ, BLINK_HOME=str(racine), PYTHONIOENCODING="utf-8"),
        )
        try:
            page = attendre("http://127.0.0.1:8899/")
            verifier(page is not None and b"blink" in page.lower(), "la page répond")
            clips = json.loads(attendre("http://127.0.0.1:8899/api/clips") or b"{}")
            verifier(len(clips.get("clips", [])) == attendu["attendus"],
                     "l'inventaire liste tous les clips, écartés compris",
                     str(len(clips.get("clips", []))))
            verifier(sum(1 for c in clips.get("clips", []) if c["excluded"]) == 1,
                     "un seul clip est marqué écarté")
            videos = json.loads(attendre("http://127.0.0.1:8899/api/videos") or b"{}")
            verifier(len(videos.get("monthly", [])) == 1,
                     "la mensuelle apparaît dans l'inventaire")
            verifier(statut("http://127.0.0.1:8899/media/monthly/../../blink.py") == 404,
                     "une traversée de chemin est refusée")
        finally:
            serveur.terminate()
            serveur.wait(timeout=10)
    finally:
        shutil.rmtree(racine, ignore_errors=True)

    print()
    if ECHECS:
        print(f"{len(ECHECS)} échec(s) : " + " ; ".join(ECHECS))
        return 1
    print("Tout est vert.")
    return 0


def attendre(url: str, essais: int = 40) -> bytes | None:
    """Interroge une adresse jusqu'à ce que le serveur soit prêt."""
    import time

    for _ in range(essais):
        try:
            with urllib.request.urlopen(url, timeout=5) as reponse:
                return reponse.read()
        except Exception:
            time.sleep(0.5)
    return None


def statut(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as reponse:
            return reponse.status
    except urllib.error.HTTPError as erreur:
        return erreur.code
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
