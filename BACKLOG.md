# Backlog

Demandes venues de Reddit (ou d'ailleurs) pas encore traitees. But : ne pas
les perdre, pas forcement les construire toutes.

- **Integration Home Assistant native.**
  Source : reddit/MoneySquare6212, r/blinkcameras, 2026-08-19. Ecarte pour
  l'instant (repondu sur Reddit) : un vrai chantier a part (config flow,
  modele d'entites, HACS), pas une extension de ce qui existe. Note ici
  pour ne pas l'oublier si la demande revient.

## Revue de code du 2026-08-20 (commit 0eab463)

Deux corrections triviales deja faites (Dockerfile CMD start, autostart.py
quoi manquant sur macOS/Linux). Le reste, verifie credible (3 affirmations
contre-verifiees dans le code avant de faire confiance au reste), pas encore
traite.

- **CSRF / requetes croisees vers l'interface locale.**
  serve.py:1230 (do_GET) et :1542 (do_POST) : aucune verification
  Origin/Host/jeton sur les routes sensibles (arm, toggle, reglages, stop,
  update...), et /api/refresh declenche telechargement+fusion en GET. Le
  plus gros morceau : jeton par processus, verif Origin/Host stricte,
  passer /api/refresh en POST.

- **Video journaliere obsolete apres exclusion totale d'une journee.**
  merge_daily.py:305, :1195, :1296. Exclure le dernier clip d'une journee la
  fait disparaitre de load_groups() : son MP4 journalier n'est plus jamais
  reconstruit ni supprime, et les hebdo/mensuelles continuent a l'agreger.

- **Verrou disque avec double proprietaire possible.**
  runtime.py:729. Course lors du nettoyage d'un verrou de PID mort (deux
  concurrents peuvent chacun croire l'avoir acquis) ; fichier vide/corrompu
  provoque en plus une boucle active infinie. Il faudrait un vrai verrou OS
  ou une suppression conditionnelle qui revalide le jeton avant d'effacer.

- **Validation MP4 trop faible.**
  merge_daily.py:71, blink_models.py:129, blink_engine.py:220. valid_mp4()
  ne regarde que les octets ftyp dans les 64 premiers octets (un fichier de
  8 octets passe), et un fichier existant invalide peut etre "adopte" sans
  telechargement. Comparer la taille HTTP/manifeste, vrai probe avant
  adoption.

- **Collisions de noms de camera.**
  merge_daily.py:66, serve.py:117. safe_name("A/B") et safe_name("A_B")
  donnent le meme resultat : journalieres et caches peuvent s'ecraser entre
  deux cameras aux noms proches. Centraliser sur l'assainissement de
  blink_models.py (deja plus robuste) + empreinte stable.

- **Plafond cloud silencieux (~475 clips).**
  blink_models.py:146. stop=20 limite blinkpy a environ 475 elements ; un
  compte actif multi-cameras peut depasser ce volume sur 30 jours sans que
  les pages anciennes soient jamais vues. Paginer jusqu'a page vide, avec
  limite de securite explicite et signalee.

- **Course a la sauvegarde de session.**
  blink_auth.py:151. Le controle updated_at se fait avant remplacement, sans
  verrou : une sauvegarde en retard peut ecraser un jeton plus recent ecrit
  entre-temps par un autre processus.

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
