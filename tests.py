"""Vérifie la chaîne vidéo et l'interface, sans compte Blink ni matériel.

L'astuce qui rend ces tests possibles partout : ffmpeg sait fabriquer des clips.
On génère donc une installation fictive, avec des clips de durées, de
définitions et de bandes-son différentes, on écrit le registre de
téléchargement que blink2video.py aurait écrit, et on déroule tout le reste. Ce qui
dépend du compte Amazon (connexion, téléchargement, direct, armement) n'est pas
testable ici et ne doit pas l'être : des identifiants n'ont rien à faire dans un
service d'intégration.

    python tests.py

Le contrôle visuel d'une notification réelle appartient à ``smoketest.py`` et
n'est jamais déclenché ici. Avec ``BLINK_TEST_BUNDLE``, tous les parcours de
ligne de commande (verbes, assemblage, serveur et arrêt) passent par le binaire
figé.

Aucun effet sur vos données : tout se passe dans un dossier temporaire désigné
par BLINK_HOME, avec un dossier de travail distinct de la racine de données.
"""

import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ECHECS = []
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Résoudre le chemin du bundle avant de quitter le dossier depuis lequel la
# suite a été lancée : les workflows lui donnent volontairement un chemin
# relatif à la racine du dépôt.
_BUNDLE_RECU = os.environ.get("BLINK_TEST_BUNDLE")
BUNDLE = str(Path(_BUNDLE_RECU).resolve()) if _BUNDLE_RECU else None

# Certains tests appellent directement des fonctions de runtime.py. Sans cette
# barrière posée avant leur import, elles écriraient leurs marques de travail et
# leurs caches dans le vrai dossier applicatif. Le CWD est lui aussi distinct :
# cela révèle les modules qui ignoreraient BLINK_HOME au profit d'un chemin
# relatif implicite, sans jamais risquer les données de l'utilisateur.
_CWD_INITIAL = Path.cwd()
_BAC_A_SABLE = tempfile.TemporaryDirectory(prefix="blink_suite_")
_BAC_RACINE = Path(_BAC_A_SABLE.name)
SUITE_HOME = _BAC_RACINE / "home"
SUITE_CWD = _BAC_RACINE / "cwd"
SUITE_HOME.mkdir()
SUITE_CWD.mkdir()
os.environ["BLINK_HOME"] = str(SUITE_HOME)
os.environ["BLINK_BOOTSTRAP"] = "none"
os.chdir(SUITE_CWD)


def _nettoyer_bac_a_sable() -> None:
    """Restaure le CWD avant d'effacer son dossier, notamment sous Windows."""
    try:
        os.chdir(_CWD_INITIAL)
    except OSError:
        pass
    _BAC_A_SABLE.cleanup()


atexit.register(_nettoyer_bac_a_sable)

import merge_daily as md  # noqa: E402 - bac à sable installé avant import
import runtime  # noqa: E402 - bac à sable installé avant import

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


def environnement_test(racine: Path) -> dict:
    """Environnement hermétique commun à chaque processus enfant.

    BLINK_NO_BROWSER et BLINK_ONBOARDING_TIMEOUT bornent l'onboarding E-01
    (blink2video.accueillir) : sans eux, un « blink2video » sans session
    enregistrée ouvrirait un vrai navigateur et attendrait jusqu'à dix
    minutes une connexion qu'aucun test automatisé ne peut fournir."""
    return dict(
        os.environ,
        BLINK_HOME=str(racine.resolve()),
        BLINK_BOOTSTRAP="none",
        PYTHONIOENCODING="utf-8",
        BLINK_NO_BROWSER="1",
        BLINK_ONBOARDING_TIMEOUT="3",
    )


def commande_blink(*arguments: str) -> list[str]:
    """Même point d'entrée en sources et dans le bundle à distribuer."""
    if BUNDLE:
        return [BUNDLE, *arguments]
    return [sys.executable, "-u", str(BASE_DIR / "blink2video.py"), *arguments]


def port_dynamique() -> int:
    """Demande au système un port loopback libre, sans numéro partagé en CI."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as prise:
        prise.bind(("127.0.0.1", 0))
        return int(prise.getsockname()[1])


def lancer(racine: Path, arguments: list) -> subprocess.CompletedProcess:
    # Avec BLINK_TEST_BUNDLE, on éprouve l'exécutable figé plutôt que les
    # sources : mêmes vérifications, mais sur ce qui sera réellement distribué.
    commande = commande_blink("merge", *arguments)
    return subprocess.run(
        commande,
        cwd=str(SUITE_CWD), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        env=environnement_test(racine),
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
            commande_blink(verbe, "--help"),
            cwd=str(SUITE_CWD), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            env=environnement_test(SUITE_HOME), check=False,
        )
        verifier(resultat.returncode == 0, f"blink2video {verbe} --help",
                 (resultat.stderr or "").strip()[:160])

    # E-01 : sans argument, blink2video n'affiche plus l'aide, il emprunte le
    # même chemin que « start » (préflight puis onboarding). Sans session
    # enregistrée dans SUITE_HOME, l'onboarding attend une connexion qui ne
    # viendra pas ; BLINK_ONBOARDING_TIMEOUT (3 s dans environnement_test)
    # borne cette attente pour que le test reste rapide et déterministe.
    sans = subprocess.run(
        commande_blink(),
        cwd=str(SUITE_CWD), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        env=environnement_test(SUITE_HOME), check=False, timeout=30,
    )
    verifier(
        sans.returncode != 0 and "Aucune session Blink valide" in sans.stdout,
        "sans argument, l'onboarding démarre", sans.stdout[-200:],
    )

    # Port dynamique ici : le port par défaut peut être occupé par une vraie
    # instance sur la machine qui fait tourner ce test, ce qui abrégerait
    # l'onboarding par un échec de port plutôt que par le délai qu'on veut
    # précisément vérifier. « start » suit le même chemin que « sans
    # argument » (blink2video.route), donc l'équivalence reste couverte.
    avec_start = subprocess.run(
        commande_blink("start", "--port", str(port_dynamique())),
        cwd=str(SUITE_CWD), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        env=environnement_test(SUITE_HOME), check=False, timeout=30,
    )
    # « Abandon » : mot commun, sans accent, aux deux messages de fin de
    # délai d'accueillir() (port qui ne répond pas, ou authentification
    # jamais confirmée). Volontairement pas de comparaison sur le message
    # complet : l'exécutable PyInstaller figé peut sortir ses caractères
    # accentués dans un encodage que ce test décode incorrectement (observé
    # en CI, « Délai » devenant illisible), sans que le comportement réel du
    # programme soit en cause — un mot ASCII commun aux deux issues suffit à
    # prouver que le délai est respecté et qu'aucun serveur temporaire ne
    # reste actif (5.16), sans dépendre du bon décodage des accents.
    verifier("Abandon" in avec_start.stdout,
             "start respecte le délai d'onboarding", avec_start.stdout[-200:])

    aide = subprocess.run(
        commande_blink("--help"),
        cwd=str(SUITE_CWD), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        env=environnement_test(SUITE_HOME), check=False,
    )
    verifier(aide.returncode == 0 and "Verbes :" in aide.stdout,
             "--help affiche l'aide sans onboarding", aide.stdout[-200:])
    manquants = [v for v in runtime.VERBES if v not in aide.stdout]
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
    environnement = environnement_test(maison)
    port = port_dynamique()
    marque = f"controle-stop-{os.getpid()}-{time.time_ns()}"
    md.save_json(maison / "Blink_Clips" / md.DOWNLOAD_STATE, {
        "version": 1,
        "clips": {
            marque: {
                "hub": "Test",
                "camera": marque,
                "created_at": "2026-08-13T12:00:00+00:00",
                "path": f"{marque}/2026-08/{marque}.mp4",
                "bytes": 0,
            },
        },
    })
    donnees = {}
    parent = subprocess.Popen(
        commande_blink("serve", "--port", str(port), "merge", "--loop", "60"),
        cwd=str(SUITE_CWD), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, env=environnement,
    )
    try:
        fiches = maison / runtime.INSTANCES
        for _ in range(60):
            trouvees = sorted(fiches.glob("*.json")) if fiches.is_dir() else []
            for fiche in trouvees:
                candidate = json.loads(fiche.read_text(encoding="utf-8"))
                if candidate.get("enfants"):
                    donnees = candidate
                    break
            if donnees.get("enfants"):
                break
            time.sleep(0.5)
        else:
            verifier(False, "l'instance dépose sa fiche")
            return
        verifier(True, "l'instance dépose sa fiche",
                 f"{len(donnees['enfants'])} enfant(s)")

        enfants = [int(numero) for numero in donnees.get("enfants") or []]
        vivants_avant = [numero for numero in enfants
                         if runtime.processus_vivant(numero)]
        verifier(len(enfants) == 2,
                 "la composition lance les deux enfants attendus",
                 f"{len(enfants)} enfant(s), attendu 2")
        verifier(vivants_avant == enfants,
                 "tous les enfants attendus vivent avant l'arrêt",
                 f"vivants {vivants_avant}, attendus {enfants}")

        adresse = f"http://127.0.0.1:{port}"
        page = attendre(
            adresse + "/", processus=parent,
            attendu=lambda corps: (
                b"blink2video" in corps.lower()
                and runtime.VERSION.encode("utf-8") in corps
            ),
        )
        verifier(page is not None,
                 "le serveur de la composition expose la version attendue")
        inventaire_brut = attendre(
            adresse + "/api/clips", processus=parent,
            attendu=lambda corps: marque.encode("utf-8") in corps,
        )
        try:
            inventaire = json.loads(inventaire_brut or b"{}")
        except json.JSONDecodeError:
            inventaire = {}
        verifier(any(clip.get("camera") == marque
                     for clip in inventaire.get("clips", [])),
                 "l'endpoint répond depuis le BLINK_HOME attendu")

        # « stop --help » doit s'expliquer, pas agir : tant qu'il agissait,
        # test_verbes arrêtait l'instance réelle de la machine à chaque passage,
        # en demandant l'aide de chaque verbe.
        aide = subprocess.run(commande_blink("stop", "--help"), cwd=str(SUITE_CWD),
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=environnement,
                              check=False)
        verifier(aide.returncode == 0
                 and runtime.processus_vivant(int(donnees["pid"])),
                 "« stop --help » explique sans arrêter")

        arret = subprocess.run(commande_blink("stop"), cwd=str(SUITE_CWD),
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
        for numero in donnees.get("enfants") or []:
            if runtime.processus_vivant(int(numero)):
                runtime.arreter_processus(int(numero), avec_descendance=True)
        if parent.poll() is None:
            parent.kill()
        try:
            parent.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        shutil.rmtree(maison, ignore_errors=True)


def test_cadence_cible() -> None:
    """Une cadence mesurée au millième ne doit pas relever la cible.

    Un clip venu du cloud, mesuré à 30,11 images/s au lieu de 30,0, a suffi à
    invalider les trente-deux segments déjà encodés d'une caméra. La montée doit
    répondre à une vraie différence de cadence, pas à un artefact de mesure."""
    print("\nCadence cible")
    def faux(fps):
        return md.ClipInfo(None, Path("x.mp4"), 1.0, 1920, 1080, fps, False)

    registre = {"cameras": {}}
    md.camera_target(registre, "jardin", [faux(30.0)])
    _, _, apres = md.camera_target(registre, "jardin", [faux(30.11)])
    verifier(apres == 30.0, "un écart de mesure ne relève pas la cadence", str(apres))

    _, _, monte = md.camera_target(registre, "jardin", [faux(60.0)])
    verifier(monte == 60.0, "une vraie montée est suivie", str(monte))

    _, _, garde = md.camera_target(registre, "jardin", [faux(15.0)])
    verifier(garde == 60.0, "la cible ne redescend jamais", str(garde))


def test_relance() -> None:
    """Chaque verbe sait relancer l'outil sur un programme qui existe.

    Le marqueur du point d'entrée dans la table des verbes servait aussi à
    fabriquer un nom de fichier : après le renommage en blink2video.py, la
    surveillance lançait « blink.py », absent, et n'a plus rien téléchargé
    pendant des heures sans que rien ne le signale."""
    import runtime

    print("\nRelance sur un autre verbe")
    if BUNDLE:
        # Un bundle n'expose naturellement aucun chemin vers ses modules
        # internes. Sa vraie relance est éprouvée dans test_arret : le binaire
        # doit y engendrer lui-même serve et merge, dont on vérifie ensuite les
        # processus et l'endpoint. Inspecter runtime.py ici ne testerait que les
        # sources qui ont servi à le construire.
        verifier(Path(BUNDLE).is_file(),
                 "le point d'entrée du bundle à relancer existe", BUNDLE)
        return

    manquants = []
    for verbe in runtime.VERBES:
        commande = runtime.self_command(verbe)
        programme = Path(commande[2])
        if not programme.is_file():
            manquants.append(f"{verbe} -> {programme.name}")
    verifier(not manquants, "chaque verbe vise un fichier existant",
             " ; ".join(manquants))


def test_notification() -> None:
    """La notification se construit sans émettre de notification réelle.

    Son contrôle visuel est explicitement réservé à ``smoketest.py``. Ici, les
    appels au système sont capturés : un passage automatique ne doit ni afficher
    un toast, ni écrire l'identité dans le registre Windows."""
    import runtime

    print("\nNotification simulée")
    appels = []
    declarations = []
    vrai_lancer = runtime.lancer
    vraie_declaration = runtime._declarer_identite

    def faux_lancer(commande, **options):
        appels.append((list(commande), dict(options)))
        return subprocess.CompletedProcess(commande, 0)

    runtime.lancer = faux_lancer
    runtime._declarer_identite = lambda: declarations.append(runtime.APP_ID)
    try:
        runtime.toast("blink2video", "Test automatique, aucune action requise.")
        verifier(True, "la notification se prépare sans exception")
    except Exception as erreur:
        verifier(False, "la notification se prépare sans exception",
                 f"{type(erreur).__name__}: {erreur}")
    finally:
        runtime.lancer = vrai_lancer
        runtime._declarer_identite = vraie_declaration

    verifier(runtime.APP_ID == "blink2video",
             "l'identité applicative est déclarée dans le code", runtime.APP_ID)
    if sys.platform == "win32":
        verifier(declarations == [runtime.APP_ID],
                 "la déclaration Windows aurait été demandée une fois",
                 str(declarations))
        verifier(len(appels) == 1 and appels[0][0][0].lower() == "powershell",
                 "la commande de notification Windows est construite une fois",
                 str(appels))
    else:
        verifier(not declarations,
                 "aucune déclaration Windows n'est tentée sur cette plateforme")


def test_avancement() -> None:
    """L'avancement publié par un calcul est lisible, et disparaît à la fin.

    C'est le seul lien entre une boucle de fond et l'interface : si la marque
    survivait à la fin du calcul, le bouton Actualiser resterait inactif jusqu'à
    l'expiration du délai de péremption, soit un quart d'heure."""
    import runtime

    print("\nAvancement d'un calcul")
    try:
        runtime.travail("Assemblage des vidéos", 3, 12)
        etat = runtime.travail_en_cours()
        verifier(etat.get("quoi") == "Assemblage des vidéos"
                 and etat.get("fait") == 3 and etat.get("total") == 12,
                 "l'avancement se relit tel qu'il a été publié", str(etat))
    finally:
        runtime.fin_travail()
    verifier(runtime.travail_en_cours() == {},
             "la marque disparaît quand le calcul se termine")


def test_mise_a_jour() -> None:
    """La détection d'une version publiée, et son unique décision.

    Tout le reste de la mise à jour touche à l'installation elle-même et ne se
    vérifie qu'en la faisant. Ce qui se teste ici est la seule décision prise
    sans surveillance : cette version est-elle plus récente que la nôtre ? Une
    comparaison de chaînes rangerait 0.5.10 avant 0.5.9, et l'outil ne
    proposerait jamais la mise à jour."""
    import maj
    import runtime

    print("\nMise à jour")
    verifier(maj._numeros("v0.5.10") > maj._numeros("0.5.9"),
             "0.5.10 est plus récent que 0.5.9")
    verifier(maj._numeros("0.5.3") == maj._numeros("v0.5.3"),
             "le « v » du nom d'étiquette ne compte pas")

    assets = [{"name": "blink2video-linux-x86_64.tar.gz", "size": 1,
               "browser_download_url": "u1"},
              {"name": "blink2video-windows-x86_64.zip", "size": 2,
               "browser_download_url": "u2"},
              {"name": "blink2video-macos-arm64.zip", "size": 3,
               "browser_download_url": "u3"}]
    choisie = maj._archive_de_ce_systeme(assets)
    attendu = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    verifier(attendu in choisie.get("nom", ""),
             "l'archive choisie est celle de ce système", choisie.get("nom", "aucune"))

    # Sans réseau : la réponse ne doit venir que du cache, et une version égale
    # à la nôtre ne doit rien proposer.
    verifier(maj.disponible(reseau=False) == {}
             or maj._numeros(maj.disponible(reseau=False)["version"])
             > maj._numeros(runtime.VERSION),
             "aucune mise à jour n'est proposée vers une version plus ancienne")

    # Le remplacement des fichiers, sur une installation factice : c'est le seul
    # moment où la mise à jour peut casser quelque chose, et ce quelque chose
    # serait les clips de quelqu'un.
    base = Path(tempfile.mkdtemp(prefix="blink_maj_"))
    try:
        installe, neuf = base / "installe", base / "neuf"
        (installe / "_internal").mkdir(parents=True)
        (installe / "_internal" / "vieux.dll").write_text("ancien")
        (installe / maj._executable(installe).name).write_text("ancien programme")
        (installe / "Blink_Clips").mkdir()
        (installe / "Blink_Clips" / "jardin.mp4").write_text("clip précieux")
        (neuf / "_internal").mkdir(parents=True)
        (neuf / "_internal" / "neuf.dll").write_text("neuf")
        (neuf / maj._executable(neuf).name).write_text("nouveau programme")

        verifier(maj._permuter(neuf, installe), "le remplacement aboutit")
        verifier(maj._executable(installe).read_text() == "nouveau programme",
                 "le programme est bien celui de la nouvelle version")
        verifier((installe / "_internal" / "neuf.dll").is_file()
                 and not (installe / "_internal" / "vieux.dll").is_file(),
                 "les fichiers internes ont été remplacés en bloc")
        verifier((installe / "Blink_Clips" / "jardin.mp4").read_text() == "clip précieux",
                 "les données de l'utilisateur n'ont pas bougé")
        maj._nettoyer(installe)
        verifier(not list(installe.glob("*.ancien")),
                 "les fichiers écartés sont effacés au passage suivant")

        # Panne au milieu du remplacement : l'installation doit se retrouver
        # dans l'état où elle était, faute de quoi il ne resterait qu'un dossier
        # à moitié neuf, incapable de démarrer et sans moyen de le dire.
        installe2, neuf2 = base / "installe2", base / "neuf2"
        (installe2 / "_internal").mkdir(parents=True)
        (installe2 / maj._executable(installe2).name).write_text("ancien programme")
        (neuf2 / "_internal").mkdir(parents=True)
        (neuf2 / maj._executable(neuf2).name).write_text("nouveau programme")
        vrai_poser, appels = maj._poser, []

        def poser_qui_lache(source, cible):
            appels.append(source)
            if len(appels) > 1:
                raise OSError("panne simulée en plein remplacement")
            return vrai_poser(source, cible)

        maj._poser = poser_qui_lache
        try:
            verifier(not maj._permuter(neuf2, installe2),
                     "une panne en cours de remplacement est signalée")
        finally:
            maj._poser = vrai_poser
        verifier(maj._executable(installe2).read_text() == "ancien programme"
                 and (installe2 / "_internal").is_dir(),
                 "après une panne, la version précédente est remise en place")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main() -> int:
    test_verbes()
    test_mise_a_jour()
    test_notification()
    test_avancement()
    test_relance()
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
        port = port_dynamique()
        adresse = f"http://127.0.0.1:{port}"
        serveur = subprocess.Popen(
            commande_blink("serve", "--port", str(port)),
            cwd=str(SUITE_CWD), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environnement_test(racine),
        )
        try:
            # Le registre fait foi : le test d'horodatage y a ajouté un clip.
            registre = md.load_json(racine / "Blink_Clips" / md.DOWNLOAD_STATE, {})
            attendus = len(registre.get("clips") or {})
            identite_témoin = next(iter(registre["clips"].values()))["path"]
            page = attendre(
                adresse + "/", processus=serveur,
                attendu=lambda corps: (
                    b"blink2video" in corps.lower()
                    and runtime.VERSION.encode("utf-8") in corps
                ),
            )
            verifier(page is not None,
                     "la page répond avec la version attendue")
            # ?all=1 : ce test vérifie l'inventaire complet, or les clips
            # synthétiques du test d'horodatage sont répartis sur plusieurs
            # mois, hors de la fenêtre par défaut de /api/clips (serve.py).
            clips_bruts = attendre(
                adresse + "/api/clips?all=1", processus=serveur,
                attendu=lambda corps: identite_témoin.encode("utf-8") in corps,
            )
            verifier(clips_bruts is not None,
                     "l'endpoint clips vient du serveur attendu")
            clips = json.loads(clips_bruts or b"{}")
            verifier(len(clips.get("clips", [])) == attendus,
                     "l'inventaire liste tous les clips, écartés compris",
                     f"{len(clips.get('clips', []))} au lieu de {attendus}")
            verifier(sum(1 for c in clips.get("clips", []) if c["excluded"]) == 1,
                     "un seul clip est marqué écarté")
            videos = json.loads(attendre(adresse + "/api/videos", processus=serveur)
                                or b"{}")
            verifier(len(videos.get("monthly", [])) == 1,
                     "la mensuelle apparaît dans l'inventaire")
            verifier(statut(adresse + "/media/monthly/../../blink2video.py") == 404,
                     "une traversée de chemin est refusée")
        finally:
            if serveur.poll() is None:
                serveur.terminate()
            try:
                serveur.wait(timeout=10)
            except subprocess.TimeoutExpired:
                serveur.kill()
                serveur.wait(timeout=10)
    finally:
        shutil.rmtree(racine, ignore_errors=True)

    print()
    if ECHECS:
        print(f"{len(ECHECS)} échec(s) : " + " ; ".join(ECHECS))
        return 1
    print("Tout est vert.")
    return 0


def attendre(url: str, essais: int = 40, processus=None,
             attendu=None) -> bytes | None:
    """Attend le bon serveur, et s'arrête immédiatement s'il est déjà mort."""
    for _ in range(essais):
        if processus is not None and processus.poll() is not None:
            return None
        try:
            with urllib.request.urlopen(url, timeout=1) as reponse:
                corps = reponse.read()
            if attendu is None or attendu(corps):
                return corps
        except Exception:
            pass
        time.sleep(0.25)
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
