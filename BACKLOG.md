# Backlog

Ce qui est ouvert, rien d'autre : ce qui est fait est dans BACKLOG-FAIT.md, mot pour mot.
Une piste notée ailleurs (rapport de mutualisation de nico579-commons, issues, mémoire de
session) est aussi listée ici, avec un renvoi : le BACKLOG est l'endroit où l'on regarde pour
savoir ce qui reste. Dernier nettoyage : 2026-10-07 (décisions du jour : Electron écarté, audit laissé en l'état).

## À décider ou à faire

- **Test hebdomadaire des fournisseurs de lidar2map : il reste la clé de fi-maanmittauslaitos.**
  Le workflow « Smoke providers » (lundi) était rouge chaque semaine depuis le 2026-08-17, sans alerte.
  Réparé le 2026-10-07 (lidar2map 1.60.2) : de-sh (le serveur n'envoie pas son certificat intermédiaire,
  embarqué désormais), es-euskadi (le service veut le nom de la couverture, plus l'index) et ca-quebec
  (c'était le test, qui prenait une feuille voisine hors zone). Reste fi : la clé du secret
  `FI_NLS_API_KEY` est refusée (HTTP 403, « invalid authorization »). Clé gratuite à renouveler
  (maanmittauslaitos.fi/rajapinnat/api-avaimen-ohje), à mettre dans le secret du dépôt lidar2map, puis
  retirer fi de `--skip` dans `.github/workflows/smoke.yml`. Aucune alerte n'est envoyée quand ce
  workflow échoue (issue automatique ou message : non décidé).

- **Signature de code des exécutables.** Candidature SignPath Foundation soumise le 2026-08-29, en
  attente de réponse.

## En attente d'un retour ou d'un cas réel

- **Audit du 2026-10-02 (0.15.7) : 13 constats restants, aucun correctif prévu.**
  Décision de Nico du 2026-10-07 : on en reste là ; à traiter seulement si l'un se présente.
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
  Si l'un se présente, B05 et B07 sont les plus plausibles en usage réel. Rien ne se corrige sans
  demande de Nico (audit veut dire rapport).

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
- **Sélecteur FR / EN et panneau Réglages de blink2video** (décision du 2026-10-07). Les trois autres
  applications ont le sélecteur commun (`/nico579-langue.js`, route `/api/langue`, choix gardé côté
  serveur) et le bouton ⚙ commun. blink2video garde les siens : sa langue est gardée par navigateur
  (`localStorage`, un téléphone et un PC peuvent différer) et recopiée au serveur pour le menu de
  l'icône ; sa page porte une CSP à nonce et son serveur ne sert aucun fichier commun ; il a deux
  groupes FR / EN (en-tête et fenêtre de connexion) et son propre panneau, déjà riche, avec la
  vérification des mises à jour et le démarrage automatique. Les unifier changerait son comportement
  pour un gain nul.
