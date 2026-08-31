# Backlog

Demandes venues de Reddit (ou d'ailleurs) pas encore traitees. But : ne pas
les perdre, pas forcement les construire toutes.

- **Integration Home Assistant native.**
  Source : reddit/MoneySquare6212, r/blinkcameras, 2026-08-19. Ecarte pour
  l'instant (repondu sur Reddit) : un vrai chantier a part (config flow,
  modele d'entites, HACS), pas une extension de ce qui existe. Note ici
  pour ne pas l'oublier si la demande revient.

- **Indicateur de mise a jour pas assez visible.**
  Source : reddit/SR_gAr, r/blinkcameras, 2026-08-20. A dit avoir ete
  "perdu dans toutes les infos" et ne pas avoir vu qu'une mise a jour etait
  disponible, a cru manquer quelque chose. Signal faible (un seul retour)
  mais a surveiller : si ca revient, revoir la visibilite du bouton/de la
  bascule "Mettre a jour" dans reglages (et maintenant dans le menu de
  l'icone tray, 28.58).

- **Tester-XR.exe affiche une "blink2video version" perimee dans son
  rapport.** Source : reddit/cutthin, auteur de la demande initiale de prise
  en charge du Sync Module XR et des tests sur materiel reel, 2026-08-31. Le
  rapport du testeur XR (r2, tag xr-local-storage-test-r2) annoncait "blink2video
  version: 0.10.6" alors que l'utilisateur confirmait tourner sous 0.10.08.
  Confirme via `gh release list` : le tag r2 a ete publie le
  2026-08-31T10:01Z, alors que v0.10.7 et v0.10.8 sont sortis apres (12:23Z
  et 12:39Z le meme jour) sans rebuild du testeur. Ce n'est pas une erreur
  de l'utilisateur : diagnostic_xr.py:88 lit `runtime.VERSION` au moment du
  build PyInstaller de Tester-XR.exe (`build_xr_tester.py`/`xr_tester.spec`),
  qui ne sont references ni dans `deploy.py` ni dans
  `.github/workflows/*.yml` (verifie, zero occurrence) - ca redivergera a
  chaque bump tant que ce n'est pas rattache. Deux corrections possibles,
  pas exclusives : rebuild+republish Tester-XR a chaque version (mecanique
  a ajouter au pipeline), ou faire lire au testeur la version reelle du
  blink2video.exe voisin plutot que sa propre runtime.VERSION figee au
  build.

## Revue de code du 2026-08-20 (commit 0eab463)

Les onze bugs numerotes de la revue sont tous traites (28.59 a 28.68) :
Dockerfile CMD start, autostart.py quoi manquant, CSRF/Origin, verrou
disque a double proprietaire, validation MP4/adoption, trois copies
divergentes de safe_name, plafond cloud silencieux, course a la
sauvegarde de session, reglages JSON mal types, surveillance watch.py
(batterie au premier passage + clips ecartes). Restent deux points
reformules ci-dessous, volontairement pas ceux d'origine - chacun touche
une decision de conception plutot qu'un simple oubli, a trancher avec
l'utilisateur - et le lot d'optimisations, non urgentes.

- **Journalieres/hebdo/mensuelles sans distinction si deux cameras se
  nettoient pareil.** (Reformule apres 28.64 : la derive entre les trois
  copies de safe_name est fermee, ceci est ce qui restait reellement.)
  merge_daily.py, etapes de regroupement : `safe_name(camera)` sert de nom
  de dossier ET de fichier sans aucun suffixe distinctif, contrairement a
  `target_path()` (clips bruts) qui en a un. Deux cameras dont le nom brut
  differe mais se nettoie pareil ("Garage" / "Garage!") verraient leurs
  videos assemblees atterrir au meme endroit. Rare en pratique (exige une
  coincidence de nommage precise), et changer le nommage des dossiers de
  sortie toucherait des installations existantes deja organisees autour de
  `Blink_Daily/<camera>/...` - decision a prendre avec l'utilisateur, pas
  a trancher seule.

- **Silence d'une camera n'ayant jamais enregistre.**
  (Reformule apres 28.68 : les deux autres defauts de watch.py sont
  fermes, ceci est ce qui restait reellement.) Le controle de silence
  prolonge (`compare()`) ne visite que les cameras presentes dans
  `last_clip` (au moins un clip, meme ecarte, un jour) - une camera qui
  n'a jamais rien enregistre depuis son installation n'y entre jamais,
  quelle que soit la duree. Fermer ce cas demande un point d'ancrage
  temporel qui n'existe pas encore (depuis quand cette camera n'a-t-elle
  rien enregistre ?) : un suivi d'etat a ajouter a WATCH_STATE (horodater
  la premiere observation d'une camera sans historique), pas un correctif
  d'une ligne - a concevoir plutot qu'a improviser au milieu d'une serie
  de corrections.

- **Optimisations identifiees (pas des bugs, pas urgentes).**
  Registre reecrit en entier a chaque clip (quadratique sur un lot) ;
  known_identities() reparcourt tout a chaque vignette (cache par mtime
  utile) ; telechargements cloud charges entierement en memoire
  (response.read(), a chunker) ; cache _DURATIONS qui ne purge jamais ses
  anciennes cles ; dependances non epinglees alors que le code touche des
  attributs prives de blinkpy ; bootstrap qui reutilise l'environnement
  global au lieu de rester isole ; plusieurs helpers JSON divergents,
  a centraliser (meme esprit que safe_name, deja fait en 28.64).
