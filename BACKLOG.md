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

## Revue de code du 2026-08-20 (commit 0eab463)

Huit corrections deja faites (Dockerfile CMD start, autostart.py quoi
manquant, CSRF/Origin, verrou disque a double proprietaire, validation
MP4/adoption, trois copies divergentes de safe_name, plafond cloud
silencieux, course a la sauvegarde de session) : 28.59 a 28.66. Le reste,
verifie credible (3 affirmations contre-verifiees dans le code avant de
faire confiance aux autres), pas encore traite.

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

- **Reglages JSON valides mais mal types.**
  runtime.py:73. blink_reglages.json contenant [] provoque un
  AttributeError (reproduit) ; nombres hors plage et chaines-comme-booleens
  mal geres aussi. Ecritures de reglages/passages a rendre atomiques et
  validees.

- **Surveillance (watch.py) incomplete.**
  watch.py:102, :151. Une batterie deja faible des le premier passage ne
  declenche aucune alerte (l'etat precedent doit valoir "ok") ; les clips
  exclus sont ignores pour la derniere activite (fausse alerte de silence
  possible) ; une camera n'ayant jamais enregistre n'entre jamais dans ce
  controle.

- **Optimisations identifiees (pas des bugs, pas urgentes).**
  Registre reecrit en entier a chaque clip (quadratique sur un lot) ;
  known_identities() reparcourt tout a chaque vignette (cache par mtime
  utile) ; telechargements cloud charges entierement en memoire
  (response.read(), a chunker) ; cache _DURATIONS qui ne purge jamais ses
  anciennes cles ; dependances non epinglees alors que le code touche des
  attributs prives de blinkpy ; bootstrap qui reutilise l'environnement
  global au lieu de rester isole ; trois implementations de safe_name (et
  plusieurs helpers JSON) deja divergentes, a centraliser.
