# Backlog

Ce qui est ouvert, rien d'autre : ce qui est fait est dans BACKLOG-FAIT.md, mot pour mot.
Une piste notée ailleurs (rapport de mutualisation de nico579-commons, issues, mémoire de
session) est aussi listée ici, avec un renvoi : le BACKLOG est l'endroit où l'on regarde pour
savoir ce qui reste. Dernier nettoyage : 2026-10-07.

## À étudier ou à décider

- **Étude Electron pour les quatre applications.** Demande de Nico du 2026-10-02 :
  Electron (https://www.electronjs.org/fr/) peut-il simplifier les quatre projets ? Aucune étude
  n'est faite, rien n'est décidé. Cadre dans nico579-commons, ANALYSE-MUTUALISATION-2026-09-29.md,
  §11 : ce qu'il remplacerait (serveur local et navigateur, icône et menu, raccourci et démarrage
  automatique, installateur et mise à jour) et ce qu'il ne remplace pas (le moteur Python :
  blinkpy, ffmpeg, aiortc, GPX, LiDAR). Avis préliminaire du 2026-10-07, à confirmer par une étude
  écrite : non. Chaque application embarquerait son Chromium et son Node (de l'ordre de 150 à 200 Mo
  de plus, quatre fois) sans rien retirer ; l'icône sous GNOME Wayland et la mise à jour automatique
  sont déjà résolues par le commun ; la mise à jour d'Electron suppose des programmes signés (la
  candidature SignPath est en attente) ; Electron ne tourne plus sous Windows 7, que l'édition
  « legacy » vise. Seul vrai gain : une fenêtre à soi plutôt qu'un onglet, donc plus de navigateur
  récent exigé pour le direct WebRTC. Alternative à comparer : un raccourci en mode application
  du navigateur. À faire maintenant que la mutualisation est terminée.

- **Audit du 2026-10-02 (0.15.7) : 13 constats restants, à décider par Nico.**
  Le dossier est `Documents\blink\audit\2026-10-02` (ignoré par git, jamais `git add -A` ;
  son `changements.patch` de 246 Ko est une donnée, à ne jamais appliquer sans ordre). Analysé le
  2026-10-06 sur la 0.18.1 : les scripts de l'audit rejoués en mesure seule, rapport, aucune
  correction. Déjà corrigés : B02 (PR #72), O04 (FFmpeg figé), O05 (clip USB réel), B03 et B13
  (0.18.2), B10 (0.18.3). Restent, tous reproduits et
  sans cas reel connu, a traiter si l'un se presente :
  B01 reconstruction echouee qui reduit semaine/mois (se repare seul au passage
  suivant reussi, verifie) ; B04 verrou vide ou `.purge` abandonne jamais repris ;
  B05 reponse 416 sans Content-Length (client en attente) ; B06 sauvegarde
  obsolete qui efface `source_deleted` ; B07 sessions HTTP non fermees apres une
  reconnexion echouee ; B08 file WebRTC pleine qui perd le signal de fin ; B09
  launchctl refuse annonce comme installe (macOS) ; B11 marqueurs de passage
  concurrents qui se remplacent ; B12 deux preparations de mise a jour dont la
  seconde efface la premiere ; B14 identite de processus illisible = verrou
  repris (CHOIX ASSUME du 2026-09-29 pour les pannes apres redemarrage : ne pas
  inverser sans accord) ; O01 types RTP > 127 au 9e profil H.264 ; O02 retour a
  zero des PTS MPEG a 33 bits.
  Mon avis : B05 (client bloqué sur une réponse 416 sans Content-Length) et B07 (sessions HTTP
  non fermées après une reconnexion échouée) sont les plus plausibles en usage réel ; le reste peut
  attendre qu'un cas se présente. Rien ne se corrige sans demande de Nico (audit veut dire rapport).

- **Test hebdomadaire des fournisseurs de lidar2map en échec chaque semaine depuis le 2026-08-17,
  et personne n'est prévenu.** Workflow « Smoke providers » (lundi). Dernier passage, le 2026-10-05 :
  47 réussis, 3 en échec : ca-quebec (la dalle de téléchargement est absente), de-sh (
  CERTIFICATE_VERIFY_FAILED, « unable to get local issuer certificate » : cause à établir, peut-être
  un certificat intermédiaire que le magasin certifi ne complète pas) et fi-maanmittauslaitos
  (HTTP 403 ; mes notes du 2026-09-26 y voyaient la clé d'API). Deux
  décisions : réparer ou retirer ces trois fournisseurs, et faire que l'échec du workflow ouvre une
  issue ou envoie un message, pour qu'une source cassée ne reste pas des semaines sans qu'on le sache.

- **Signature de code des exécutables.** Candidature SignPath Foundation soumise le 2026-08-29, en
  attente de réponse. Conditionne aussi les mises à jour automatiques d'Electron (voir ci-dessus).

- **Brouillon de release v0.11.5, orphelin.** Les trois tentatives de publication de septembre ont
  échoué (corrigé en v0.11.6, voir BACKLOG-FAIT.md) ; il reste un brouillon `v0.11.5` avec 6 fichiers
  sur GitHub, sans tag. À supprimer si Nico est d'accord (décision laissée à lui, rien n'a été effacé).

## En attente d'un retour ou d'un cas réel

- **Issue #55 (Björn) : « USB storage only detects clips from one camera ».** Cause trouvée et
  corrigée en 0.15.7 : le Sync Module écrit les noms de caméra en ASCII (le ü est supprimé) et
  blinkpy les cherchait avec ü. La règle réelle pour les autres accents n'est pas confirmée ;
  attendre le retour de Björn avant de fermer.

- **Issue #40 (Markus) : le nombre d'images par ligne change d'une vue à l'autre à taille de
  fenêtre égale.** Markus pense que cela dépend de la largeur de la ligne d'informations du haut. À
  vérifier : si cela se confirme, c'est un défaut de mise en page, pas un choix. Tri, taille et
  masquage sont en 0.16.0.

- **Vignettes : une borne sur la file d'attente de serve.** Le reste de l'entrée, faite en 0.18.0 :
  `send_thumb()` laisse chaque requête en attente occuper un fil du serveur. Le test à froid de Joël
  sur ses vrais clips dira si elle est encore utile.

- **Icône sous GNOME Wayland : la voir dans une vraie session.** Livrée en 0.18.0 et essayée dans la
  VM Ubuntu 26.04 avec un gnome-shell sans écran (enregistrement auprès du watcher, GetLayout,
  Event) ; le rendu dans une vraie session graphique n'a pas été vu.

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
  Toujours ouvert au 2026-10-07 : `diagnostic_xr.py` lit encore `runtime.VERSION` figée au build, et
  ni `deploy.py` ni les workflows ne reconstruisent le testeur.

## À ne faire que si la demande revient

- **Integration Home Assistant native.**
  Source : reddit/MoneySquare6212, r/blinkcameras, 2026-08-19. Ecarte pour
  l'instant (repondu sur Reddit) : un vrai chantier a part (config flow,
  modele d'entites, HACS), pas une extension de ce qui existe. Note ici
  pour ne pas l'oublier si la demande revient.

- **Optimisations identifiees (pas des bugs, pas urgentes).**
  Registre reecrit en entier a chaque clip (quadratique sur un lot) ;
  known_identities() reparcourt tout a chaque vignette (cache par mtime
  utile) ; telechargements cloud charges entierement en memoire
  (response.read(), a chunker) ; cache _DURATIONS qui ne purge jamais ses
  anciennes cles ; dependances non epinglees alors que le code touche des
  attributs prives de blinkpy ; bootstrap qui reutilise l'environnement
  global au lieu de rester isole ; plusieurs helpers JSON divergents,
  a centraliser (meme esprit que safe_name, deja fait en 28.64).

- **Flux par camera pour Frigate / Home Assistant (RTSP via go2rtc).**
  Source : reddit/wperez3825, 2026-09-28, en reponse au "Direct continu" de
  la 0.14.6 ("great news for Blink Minis since they're plugged in").
  Demande : exposer chaque camera en flux, par exemple via go2rtc, pour que
  Frigate et Home Assistant l'enregistrent et l'affichent. Analyse du
  2026-09-28 : faisable, mais un Sync Module ne traite qu'une commande a la
  fois (voir MODULE_SLOT, serve.py), donc au mieux un direct par systeme
  Blink en meme temps ; deux directs simultanes sur un meme compte jamais
  essayes. Travail : adresse stable par camera protegee par une cle fixe
  (comme le jeton de webhook), relance continue cote serveur (elle vit
  aujourd'hui dans la page), flux en MPEG-TS plutot qu'en MP4 fragmente pour
  recoller les sessions de ~6 min, page de doc go2rtc. A essayer d'abord :
  deux directs en meme temps sur deux systemes, et go2rtc branche sur
  /live-mse. Mis de cote par Nico pour plus tard.

- **Armement par camera et par horaire (issue #40), pas retenu pour
  l'instant.**
  Source : MarkusKress, 2026-09-29, lui-meme "tres specifique, sans
  importance". Armer et desarmer des cameras choisies a des heures fixes,
  avec verification de l'etat au demarrage de blink2video. L'armement manuel
  existe deja (/api/arm, serve.py) ; ce qui manque est le planning, une
  tache de fond qui ne doit jamais laisser une camera dans le mauvais etat,
  et qui entre en conflit avec les horaires de l'application Blink. Question
  posee a Markus : que fait sa solution quand une camera ne repond pas a
  l'heure d'armement. A ne construire que si la demande se repete.

## Différences voulues entre les applications (à ne pas « corriger »)

- **Deux contextes TLS.** `blink_tls.py` donne à blinkpy un contexte strict avec le bundle certifi ;
  `_bootstrap_tls.py` de lidar2map remplace la fabrique HTTPS de tout le processus avant l'amorçage :
  deux mécanismes. `AGENTS.md` demande la prudence sur le TLS de blink2video (le relais du direct
  présente un certificat auto-signé, exception à ne jamais étendre).
- **`end_headers` de blink2video** : CSP à nonce, absente des autres applications.
- **`web_bridge.js`** : un helper de 12 lignes et sept méthodes communes, le reste est l'API de chaque
  application.
- **Phase 1 de `installer` de blink2video**, passage de main au binaire neuf, compositions lues du
  registre, sortie du service systemd : propres à blink2video par conception.
- **B14 de l'audit** : identité de processus illisible = verrou repris, choix assumé du 2026-09-29 pour
  les pannes après redémarrage, à ne pas inverser sans accord.
