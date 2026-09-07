# Benchmark sur l'hôte Windows et les caméras Blink

Essais du 6 septembre 2026, sur la machine physique, pas dans la VM. État au 7 septembre : série interrompue après 13 essais complets sur 18 prévus. Les résultats sont partiels, sans conclusion de latence absolue. Le travail a ensuite été réorienté, à la demande de l'utilisateur, vers l'intégration du réencodage comme repli dans l'application.

## Périmètre

Caméras autorisées : Salon, Terrasse1 et jardin. Portail exclu. Aucun enregistrement de vidéo, aucune modification des réglages de lecture, aucun redémarrage de l'application existante. La connexion TLS du relais Blink n'a pas été modifiée.

Le comparatif principal appelle les lecteurs `watchMse` et `watchWebRTC` de la page réellement servie par l'application déjà active sur le port 8765. Il ne remplace pas la chaîne MSE par celle du banc synthétique Win7.

Machine : Windows 10 22H2 (19045), 2 cœurs physiques / 4 processeurs logiques, environ 8 Go de RAM ; Intel UHD Graphics 620. Chrome 152.0.7977.76, profil diagnostique distinct du profil personnel, accélération graphique activée (décodage vidéo disponible selon Chrome). Le choix effectif du décodeur WebRTC est relevé séparément dans ses statistiques.

## Méthode et limites

Trois répétitions prévues par caméra et protocole. L'ordre des protocoles et des caméras est inversé lors de la deuxième répétition. Chaque essai vise 25 secondes après la première image présentée. La fenêtre de régime établi commence 5 secondes après cette première image et se termine à la fin effective de la mesure. Les reprises natives de l'application restent actives et sont comptées.

Le module est vérifié libre avant l'essai, puis le direct est explicitement arrêté et le module vérifié libre après. Les mesures au repos sont conservées sans soustraction arbitraire. Les autres applications de l'utilisateur restent ouvertes : leur activité peut affecter la charge globale de Windows.

Indicateurs :

- Démarrage : action du lecteur → premier rappel de présentation d'image ; détection non noire conservée séparément, simple heuristique et non preuve de contenu utile.
- Initialisation client : action du lecteur → première requête HTTP de direct. Cette durée n'est pas du réveil caméra.
- Arrivée vidéo → première image : journal serveur « premier octet vidéo du relais » → rappel de présentation. En réalité, ce journal est écrit après réception du premier payload vidéo complet. Cette mesure décrit le démarrage local, pas la latence en régime établi.
- FPS : progression du compteur `presentedFrames` entre rappels de présentation, par vidéo, rapportée au temps de présentation. Un rappel JavaScript manqué n'est pas automatiquement une image perdue. Les intervalles entre rappels sont un diagnostic distinct.
- CPU : intervalles pondérés par leur recouvrement avec la fenêtre utile ; serveur et ses enfants FFmpeg, navigateur et son arbre de processus dédié, Windows global. **100 % représente les quatre processeurs logiques de l'hôte**, contrairement à l'unique processeur virtuel du précédent banc Win7. Un équivalent processeur logique occupé représente 25 points de CPU ici.
- Mémoire privée : octets privés engagés par arbre de processus, en Mio ; pas une mesure de mémoire physique résidente exclusive.
- Tampon MSE : durée disponible devant la tête de lecture. WebRTC : attente moyenne dans le tampon de gigue et temps de décodage. Ces valeurs ne représentent pas la même portion de la chaîne et ne doivent pas être comparées comme deux latences de bout en bout.
- Dérive relative : progression du temps de présentation moins progression du temps média. Elle ne donne pas le niveau absolu du retard.

Il n'y a pas de référence optique dans le champ des caméras. **La latence absolue scène filmée → écran n'est donc pas mesurée.** Les PTS des images reçues ne sont pas associés à une horloge murale au relais : les présentes sondes ne mesurent pas non plus la latence locale absolue en régime établi.

Les médianes et étendues entre trois essais décrivent la variabilité observée, pas un intervalle de confiance. Une comparaison entre caméras de définition ou cadence différentes ne serait pas une comparaison à qualité égale.

## Traçabilité

La série contient 3 essais WebRTC Salon, 2 MSE Salon et 2 essais par protocole sur Terrasse1 et jardin. Le troisième MSE Salon a démarré mais n'a pas de mesure finale enregistrée : il ne compte pas comme essai complet. À la reprise du travail, aucun processus de benchmark ni Chrome diagnostique ne restait actif ; le serveur de l'utilisateur était toujours en fonctionnement et `/api/attente-module` confirmait le module libre.

Signal principal des essais complets : la médiane du démarrage **après réception du premier payload vidéo** vaut environ 1,44 s en WebRTC contre 4,17 s en MSE pour Salon ; 1,32 contre 9,04 s pour Terrasse1 ; 1,40 contre 8,92 s pour jardin. Il ne s'agit ni du délai total après clic, ni de la latence en régime établi. Les interruptions et variations de cadence constatées interdisent de conclure que WebRTC fournit systématiquement davantage de FPS. La cadence nocturne des caméras diffère de la mire synthétique 30 FPS ; les images-clés décodées observées sont espacées d'environ 2 s sur Salon et 4 s sur Terrasse1/jardin, ce qui est cohérent avec une attente de fragmentation MSE plus importante.

- `build-win7/host-cameras-real2.jsonl` : série principale sur les lecteurs de l'application active.
- `build-win7/host-cameras-pilot3.jsonl` : validation courte de l'instrumentation, exclue des agrégats principaux.
- `build-win7/host-cameras-pilot1.jsonl` et `pilot2` : erreurs de chargement de la sonde avant ouverture de caméra, exclues.
- `build-win7/host-cameras-real1.jsonl` : série interrompue, exclue des agrégats principaux, y compris son premier essai complet.
- `build-win7/host_camera_bench.py`, `host_live_browser.js`, `bench_winmetrics.py` : instrumentation. Le nom du dossier est historique ; ces essais sont exécutés sur Windows hôte.
- `direct.log` : jalons serveur, horodatés dans le fuseau Europe/Paris. Les éventuelles récupérations de jalons absents des extraits JSONL sont bornées à l'intervalle exact de l'essai et identifiées comme telles.

Le rapport Win7 a été rectifié : son banc MSE omettait `-fflags nobuffer -flags low_delay`, présents dans l'application, et envoyait les en-têtes avant la lecture du `moov`. Ses résultats synthétiques ne doivent pas être extrapolés aux caméras réelles.

## Validation du repli intégré — 7 septembre

> État ultérieur : le réencodage et son repli automatique ont été retirés de l'application à la demande de l'utilisateur. Cette section conserve les résultats historiques de l'expérimentation, pas une fonctionnalité actuellement proposée. Les fichiers de conversion et leurs tests dédiés sont archivés localement dans `build-win7/retired-baseline/` ; les rapports et mesures brutes sont conservés.

Ces essais sont des validations fonctionnelles, pas la suite du benchmark comparatif à trois répétitions. Le réencodage est désormais un repli automatique de compatibilité dans le code applicatif ; le direct natif reste prioritaire et MSE reste un choix explicite.

- Mire synthétique High 1080p30, Chrome sur l'hôte : trois cas réussis (offre complète → direct High 1080p ; offre Baseline seule → vraie conversion Baseline 720p ; conversion Baseline demandée explicitement). Chaque cas a une seule connexion TCP source et un seul nettoyage. Aucun enfant diagnostique restant. Données : `build-win7/production-fallback-smoke1.jsonl`.
- Caméra Salon réelle, même Chrome avec offre limitée à ses capacités Baseline : réponse HTTP 200, mode déclaré `baseline`, image 1280 × 720, première image après 6,58 s depuis le lancement du lecteur. Fenêtre RTC utile de 7,06 s : 106 images décodées, soit environ 15 FPS, zéro image abandonnée et zéro gel signalé par RTC. Arrêt confirmé, module libre côté navigateur et serveur. Données : `build-win7/host-cameras-fallback-salon1.jsonl`.
- Le serveur de prévisualisation utilisait les fonctions de production et le verrou disque habituel, pas le patch de laboratoire. L'application de l'utilisateur est restée active sur son port initial ; aucun réglage n'a changé. Le nouveau code nécessite son redémarrage pour y être chargé.
- `python -B -m unittest discover -q` : 536 tests réussis. `python tests.py` : chaîne vidéo/interface entièrement verte. La première exécution de la suite pendant les modifications avait échoué sur un ancien mock de badge JavaScript ; la suite finale complète passe.

Le repli conserve la cadence source et plafonne la définition à 720p, sans agrandissement. L'enregistrement demandé explicitement conserve le H.264 original. Aucun enregistrement n'a été activé pendant ces essais. Firefox/Win7 n'a pas encore été testé avec ce nouveau repli ; une offre Baseline restreinte dans Chrome ne remplace pas cette validation.
