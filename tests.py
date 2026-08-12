"""Vérifie la chaîne vidéo et l'interface, sans compte Blink ni matériel.

L'astuce qui rend ces tests possibles partout : ffmpeg sait fabriquer des clips.
On génère donc une installation fictive, avec des clips de durées, de
définitions et de bandes-son différentes, on écrit le registre de
téléchargement que blink2video.py aurait écrit, et on déroule tout le reste. Ce qui
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
import time
import urllib.request
from pathlib import Path

import merge_daily as md


BASE_DIR = Path(__file__).resolve().parent
ECHECS = []

# La console d'un runner Windows est en cp1252 : sans cela, le premier accent
# affiché fait échouer les tests eux-mêmes, ce qui laisse croire à un défaut de
# l'outil alors que c'est l'échafaudage qui casse.
for flux in (sys.stdout, sys.stderr):
    try:
        flux.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


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


# Chemin résolu : Windows cherche un exécutable relatif dans le répertoire du
# processus appelant, pas dans celui qu'on donne à l'enfant.
BUNDLE = os.environ.get("BLINK_TEST_BUNDLE")
if BUNDLE:
    BUNDLE = str(Path(BUNDLE).resolve())


def lancer(racine: Path, arguments: list) -> subprocess.CompletedProcess:
    # Avec BLINK_TEST_BUNDLE, on éprouve l'exécutable figé plutôt que les
    # sources : mêmes vérifications, mais sur ce qui sera réellement distribué.
    commande = ([BUNDLE, "merge", *arguments] if BUNDLE
                else [sys.executable, "-u", str(BASE_DIR / "merge_daily.py"), *arguments])
    return subprocess.run(
        commande,
        cwd=str(BASE_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, BLINK_HOME=str(racine), PYTHONIOENCODING="utf-8"),
        check=False,
    )


def duree(ffmpeg: str, fichier: Path) -> float:
    return md.probe_clip_info(ffmpeg, fichier)[0]


def pixels_allumes(ffmpeg: str, video: Path, seconde: float) -> int:
    """Compte les pixels non noirs dans le bas de l'image, là où s'écrit l'heure.

    Vérifier que le filtre drawtext existe ne prouve rien : une police
    introuvable ou un format de date que la libc ignore produisent une image
    vide, sans la moindre erreur. Les deux pannes se sont réellement produites.
    Seuls des pixels le prouvent."""
    resultat = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", str(seconde),
         "-i", str(video), "-frames:v", "1",
         # Bande basse de l'image, là où le cartouche est posé, en niveaux de gris.
         "-vf", "crop=iw:ih/6:0:ih*5/6,format=gray",
         "-f", "rawvideo", "-"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, check=False,
    )
    return sum(1 for octet in resultat.stdout if octet > 60)


def test_horodatage(racine: Path, ffmpeg: str) -> None:
    """Éprouve l'incrustation sur un clip volontairement noir.

    Sur une image noire, tout pixel allumé dans la zone du cartouche ne peut
    venir que de l'horodatage."""
    print("\nHorodatage réellement incrusté")
    noir = racine / "Blink_Clips" / "jardin" / "2026-03" / "2026-03-03_12-00-00Z_jardin_2000.mp4"
    noir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "color=c=black:s=640x360:d=3:r=30",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(noir)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        check=True)

    registre = md.load_json(racine / "Blink_Clips" / md.DOWNLOAD_STATE, {})
    registre["clips"]["1:jardin:2026-03-03T12:00:00+00:00"] = {
        "hub": "Test", "camera": "jardin", "created_at": "2026-03-03T12:00:00+00:00",
        "path": "jardin/2026-03/2026-03-03_12-00-00Z_jardin_2000.mp4", "bytes": 1000,
    }
    md.save_json(racine / "Blink_Clips" / md.DOWNLOAD_STATE, registre)

    avant = pixels_allumes(ffmpeg, noir, 1.0)
    verifier(avant == 0, "le clip source est bien entièrement noir", str(avant))

    sortie = lancer(racine, ["--timezone", "UTC"])
    verifier(sortie.returncode == 0, "assemblage du clip noir", sortie.stdout[-300:])

    normalise = racine / "Blink_Normalized" / "jardin" / "2026-03" / noir.name
    verifier(normalise.is_file(), "le segment normalisé existe")
    if normalise.is_file():
        apres = pixels_allumes(ffmpeg, normalise, 1.0)
        verifier(apres > 200,
                 "l'heure est réellement dessinée dans l'image",
                 f"{apres} pixels allumés, attendu plus de 200")

    journaliere = racine / "Blink_Daily" / "jardin" / "2026-03-03_jardin.mp4"
    if journaliere.is_file():
        verifier(pixels_allumes(ffmpeg, journaliere, 1.0) > 200,
                 "l'heure survit à l'assemblage de la journalière")


def test_verbes() -> None:
    """Chaque verbe de la table répond à --help.

    Le contrôle est piloté par runtime.VERBES, jamais par une liste écrite à la
    main : c'est précisément une liste parallèle, dans le workflow, qui a
    continué de citer « review » après son renommage en « serve »."""
    import runtime

    print("\nLes verbes répondent")
    for verbe in runtime.VERBES:
        resultat = subprocess.run(
            [sys.executable, str(BASE_DIR / "blink2video.py"), "--bootstrap=none",
             verbe, "--help"],
            cwd=str(BASE_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"), check=False,
        )
        verifier(resultat.returncode == 0, f"blink2video {verbe} --help",
                 (resultat.stderr or "").strip()[:160])

    sans = subprocess.run(
        [sys.executable, str(BASE_DIR / "blink2video.py"), "--bootstrap=none"],
        cwd=str(BASE_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"), check=False,
    )
    verifier(sans.returncode == 0 and "Verbes :" in sans.stdout,
             "sans argument, l'aide s'affiche", sans.stdout[-200:])
    manquants = [v for v in runtime.VERBES if v not in sans.stdout]
    verifier(not manquants, "l'aide cite tous les verbes", ", ".join(manquants))


def test_installation_neuve() -> None:
    """Une installation sans le moindre clip s'ouvre au lieu de refuser.

    Le registre n'existe qu'après le premier téléchargement : le confondre avec
    un registre corrompu faisait échouer « serve » et « merge » chez quiconque
    venait d'installer l'outil. Un registre réellement illisible, lui, doit
    toujours se dire."""
    print("\nInstallation neuve")
    neuve = Path(tempfile.mkdtemp(prefix="blink_neuve_"))
    try:
        clips = neuve / "Blink_Clips"
        verifier(md.read_registry(clips / md.DOWNLOAD_STATE) == {},
                 "registre absent : catalogue vide")

        sortie = lancer(neuve, ["--timezone", "UTC"])
        verifier(sortie.returncode == 0, "l'assemblage se termine sans matière",
                 sortie.stdout[-200:])

        clips.mkdir(parents=True, exist_ok=True)
        (clips / md.DOWNLOAD_STATE).write_text("ceci n'est pas du json",
                                               encoding="utf-8")
        try:
            md.read_registry(clips / md.DOWNLOAD_STATE)
            verifier(False, "registre corrompu : signalé")
        except RuntimeError as erreur:
            verifier("illisible" in str(erreur), "registre corrompu : signalé",
                     str(erreur))
    finally:
        shutil.rmtree(neuve, ignore_errors=True)


def test_arret() -> None:
    """« blink2video stop » arrête l'instance et tous ses verbes.

    Tuer le seul processus parent laissait ses enfants derrière lui : un
    « watch » orphelin continuait de tourner en tenant le module de
    synchronisation, invisible et increvable sans chercher son numéro."""
    import runtime

    print("\nArrêt d'une instance")
    maison = Path(tempfile.mkdtemp(prefix="blink_stop_"))
    environnement = dict(os.environ, BLINK_HOME=str(maison),
                         PYTHONIOENCODING="utf-8")
    commande = [BUNDLE] if BUNDLE else [sys.executable, "-u", str(BASE_DIR / "blink2video.py")]
    parent = subprocess.Popen(
        [*commande, "serve", "--port", "8945", "merge", "--loop", "60"],
        cwd=str(BASE_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, env=environnement,
    )
    try:
        fiches = maison / runtime.INSTANCES
        for _ in range(60):
            trouvees = sorted(fiches.glob("*.json")) if fiches.is_dir() else []
            if trouvees:
                donnees = json.loads(trouvees[0].read_text(encoding="utf-8"))
                if donnees.get("enfants"):
                    break
            time.sleep(0.5)
        else:
            verifier(False, "l'instance dépose sa fiche")
            return
        verifier(True, "l'instance dépose sa fiche",
                 f"{len(donnees['enfants'])} enfant(s)")

        # « stop --help » doit s'expliquer, pas agir : tant qu'il agissait,
        # test_verbes arrêtait l'instance réelle de la machine à chaque passage,
        # en demandant l'aide de chaque verbe.
        aide = subprocess.run([*commande, "stop", "--help"], cwd=str(BASE_DIR),
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=environnement,
                              check=False)
        verifier(aide.returncode == 0
                 and runtime.processus_vivant(int(donnees["pid"])),
                 "« stop --help » explique sans arrêter")

        arret = subprocess.run([*commande, "stop"], cwd=str(BASE_DIR),
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True,
                               encoding="utf-8", errors="replace",
                               env=environnement, check=False)
        verifier(arret.returncode == 0, "« stop » se termine sans erreur",
                 arret.stdout[-300:])

        # Le parent est un enfant de cette suite : sans ce wait, il resterait
        # zombie et compterait pour vivant. Hors tests, c'est le système qui
        # récupère.
        try:
            parent.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1)
        survivants = [numero for numero in [donnees["pid"], *donnees["enfants"]]
                      if runtime.processus_vivant(int(numero))]
        verifier(not survivants, "aucun processus ne survit",
                 "PID " + ", ".join(map(str, survivants)))
        verifier(not list(fiches.glob("*.json")) if fiches.is_dir() else True,
                 "la fiche est retirée")
    finally:
        if parent.poll() is None:
            parent.kill()
        shutil.rmtree(maison, ignore_errors=True)


def test_cadence_cible() -> None:
    """Une cadence mesurée au millième ne doit pas relever la cible.

    Un clip venu du cloud, mesuré à 30,11 images/s au lieu de 30,0, a suffi à
    invalider les trente-deux segments déjà encodés d'une caméra. La montée doit
    répondre à une vraie différence de cadence, pas à un artefact de mesure."""
    print("\nCadence cible")
    faux = lambda fps: md.ClipInfo(None, Path("x.mp4"), 1.0, 1920, 1080, fps, False)

    registre = {"cameras": {}}
    md.camera_target(registre, "jardin", [faux(30.0)])
    _, _, apres = md.camera_target(registre, "jardin", [faux(30.11)])
    verifier(apres == 30.0, "un écart de mesure ne relève pas la cadence", str(apres))

    _, _, monte = md.camera_target(registre, "jardin", [faux(60.0)])
    verifier(monte == 60.0, "une vraie montée est suivie", str(monte))

    _, _, garde = md.camera_target(registre, "jardin", [faux(15.0)])
    verifier(garde == 60.0, "la cible ne redescend jamais", str(garde))


def test_compte_des_nouveaux() -> None:
    """La surveillance sait lire le décompte que le téléchargement annonce.

    Contrat entre deux programmes, tenu par une expression régulière : c'est
    exactement ce qui s'est rompu quand le cloud est apparu, la surveillance
    comptant alors les seules sections du stockage local."""
    import watch

    print("\nDécompte des nouveaux clips")
    sortie = (BASE_DIR / "blink2video.py").read_text(encoding="utf-8")
    verifier("Nouveaux clips :" in sortie,
             "le téléchargement écrit bien cette ligne")
    verifier(watch.compter_nouveaux("bruit\nNouveaux clips : 3\nautre") == 3,
             "la surveillance la lit")
    verifier(watch.compter_nouveaux("aucune ligne de synthèse") == 0,
             "une sortie sans synthèse ne compte rien")


def test_relance() -> None:
    """Chaque verbe sait relancer l'outil sur un programme qui existe.

    Le marqueur du point d'entrée dans la table des verbes servait aussi à
    fabriquer un nom de fichier : après le renommage en blink2video.py, la
    surveillance lançait « blink.py », absent, et n'a plus rien téléchargé
    pendant des heures sans que rien ne le signale."""
    import runtime

    print("\nRelance sur un autre verbe")
    manquants = []
    for verbe in runtime.VERBES:
        commande = runtime.self_command(verbe)
        programme = Path(commande[2])
        if not programme.is_file():
            manquants.append(f"{verbe} -> {programme.name}")
    verifier(not manquants, "chaque verbe vise un fichier existant",
             " ; ".join(manquants))


def main() -> int:
    test_verbes()
    test_relance()
    test_compte_des_nouveaux()
    test_cadence_cible()
    test_installation_neuve()
    test_arret()
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
        # On compare sur des fragments sans accent : selon la console, la
        # sortie relue peut contenir des caractères de remplacement.
        verifier("0 clip(s) encod" in sortie.stdout,
                 "aucun ré-encodage inutile", sortie.stdout[-300:])
        verifier("0 cr" in sortie.stdout and "d" in sortie.stdout,
                 "aucun réassemblage inutile", sortie.stdout[-300:])

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

        test_horodatage(racine, ffmpeg)

        print("\nInterface web")
        serveur = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "serve.py"), "--port", "8899"],
            cwd=str(BASE_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ, BLINK_HOME=str(racine), PYTHONIOENCODING="utf-8"),
        )
        try:
            page = attendre("http://127.0.0.1:8899/")
            verifier(page is not None and b"blink" in page.lower(), "la page répond")
            clips = json.loads(attendre("http://127.0.0.1:8899/api/clips") or b"{}")
            # Le registre fait foi : le test d'horodatage y a ajouté un clip.
            registre = md.load_json(racine / "Blink_Clips" / md.DOWNLOAD_STATE, {})
            attendus = len(registre.get("clips") or {})
            verifier(len(clips.get("clips", [])) == attendus,
                     "l'inventaire liste tous les clips, écartés compris",
                     f"{len(clips.get('clips', []))} au lieu de {attendus}")
            verifier(sum(1 for c in clips.get("clips", []) if c["excluded"]) == 1,
                     "un seul clip est marqué écarté")
            videos = json.loads(attendre("http://127.0.0.1:8899/api/videos") or b"{}")
            verifier(len(videos.get("monthly", [])) == 1,
                     "la mensuelle apparaît dans l'inventaire")
            verifier(statut("http://127.0.0.1:8899/media/monthly/../../blink2video.py") == 404,
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
