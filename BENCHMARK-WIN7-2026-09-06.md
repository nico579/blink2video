# Benchmark Windows 7 : MSE et WebRTC — jeu de mesures complet

> Rectification du 6 septembre 2026 : le banc MSE n'est pas une reproduction exacte de l'application. Il omet `-fflags nobuffer -flags low_delay`, déjà présents dans `serve.py` lors des essais, et envoie les en-têtes avant la lecture du `moov`. Les résultats ci-dessous décrivent donc uniquement les chaînes synthétiques effectivement testées. Leur classement et leurs latences ne doivent pas être extrapolés au Live View réel de Blink. La recommandation MSE/WebRTC formulée plus bas est suspendue en attendant un benchmark distinct sur l'hôte et les caméras.

Date des essais : 6 septembre 2026. Rapport produit à partir des seuls journaux explicitement retenus.


## Conclusion pratique

Dans cette VM, le réencodage Baseline testé est une solution de compatibilité, pas une optimisation de performance. À résolution cible identique, le MSE conserve presque 30 FPS à environ 54 % de CPU total ; le WebRTC réencodé 1080p affiche environ 23 FPS à 94 % de CPU et ajoute environ 2,3 secondes au retard médian.

Le WebRTC sans réencodage évite cette dépense de conversion. Son intérêt en latence dépend notamment des fragments MSE : dans le contrôle à GOP de 2 secondes, MSE atteint 2,34 s et WebRTC direct 1,51 s, mais ce contrôle n'a qu'une répétition. Le réencodage 720p15 reste autour de 4,04 s.

Le réencodage 1080p30 testé est trop exigeant pour tenir 30 FPS dans cette VM. En revanche, ce banc ne permet pas de recommander le lecteur MSE réel de l'application plutôt que son lecteur WebRTC : sa chaîne MSE diffère de la production, comme indiqué dans la rectification en tête du rapport.

La chaîne expérimentale n'a pas été optimisée davantage pour son démarrage : elle conserve une analyse FFmpeg de 1,5 s et le tampon WebRTC de 1,2 s. Réduire ces attentes pourrait améliorer son retard ; cela ne supprimerait pas le coût CPU du décodage/réencodage. Cette piste reste à mesurer, pas démontrée par ce rapport.


## Périmètre et état des mesures

Ce banc exécute réellement le serveur vidéo et Supermium dans la VM Windows 7, avec décodage et présentation des images dans un navigateur visible. Il utilise une source synthétique horodatée, pas une caméra. Il ne mesure donc ni le réveil Blink, ni le Wi-Fi de la caméra, ni le relais cloud, ni la latence optique caméra-écran.

Essais sélectionnés : **15** ; exploitables dans les tableaux agrégés : **15/15**. Chaque mode vise trois répétitions. « — » indique une mesure absente, jamais un zéro.

| Mode | Répétitions exploitables / prévues |
| --- | --- |
| MSE copie 1080p30 | 3 / 3 |
| WebRTC natif 1080p30 | 3 / 3 |
| WebRTC réencodé 1080p30 | 3 / 3 |
| WebRTC réencodé 720p30 | 3 / 3 |
| WebRTC réencodé 720p15 | 3 / 3 |

## Méthode

Machine : Windows 7 SP1 64 bits dans VirtualBox, **1 CPU virtuel et 2 Go de RAM**. Même Supermium 144, même profil de test isolé et même fenêtre vidéo visible pour tous les modes ; accélération GPU désactivée. Supermium 144.0.7559.256 ; FFmpeg 4.2.2 ; Python 3.8.10, aiortc 1.9.0 et PyAV 12.3.0. Vidéo affichée en 800 × 450 pixels CSS. Serveur et navigateur partagent le CPU de la VM. Les résultats ne préjugent pas d'un serveur plus puissant ou d'un navigateur distant.

Source commune : mire animée H.264 High 1920 × 1080, 30 images/s, cible 2 Mbit/s, GOP de 30 images (1 seconde), sans images B. Le fichier de 75 secondes contient 2 250 images, préparées hors mesure ; leurs PTS avancent de 3 000 ticks sur une horloge à 90 kHz. Un marqueur binaire de 12 bits encode l'identifiant original de chaque image. La source est émise en temps réel ; les variants 15 FPS conservent les identifiants originaux, sans les renuméroter.

| Chaîne | Traitement mesuré |
| --- | --- |
| MSE | FFmpeg -c:v copy, sans réencodage ; analyse/probe à 1 500 000 ; MP4 fragmenté frag_keyframe+empty_moov+default_base_moof ; blocs de 16 384 octets ; SourceBuffer sequence avec append sérialisés, sans saut au bord live. |
| WebRTC natif | Piste de relais H.264 de l'application, sans décodage/réencodage serveur ; tampon normal de 1,2 s conservé ; ICE/DTLS/SRTP et décodage navigateur réels. |
| WebRTC réencodé | Décodage du même High puis libx264 Baseline, ultrafast + zerolatency, yuv420p, sans images B ; 1 thread de décodage, 1 thread de filtre et 1 thread d'encodage ; fps avant scale ; cible/max 2 Mbit/s, tampon 1 Mbit, GOP 1 s ; même piste WebRTC et tampon 1,2 s. |

Le banc emploie la piste et le pacing WebRTC de l'application, mais un serveur HTTP isolé sans Blink et sans enregistrement. Sa chaîne MSE omet les options d'entrée de production `-fflags nobuffer -flags low_delay` et envoie les en-têtes avant la lecture du `moov` ; elle n'est donc pas identique à celle de l'application. Dans ce processus de laboratoire seulement, les profils H.264 par défaut d'aiortc sont retirés avant d'enregistrer le profil réel : cela évite le conflit de filtrage strict entre les profils Baseline équivalents 42c01f et 42e01f. La vidéo High est réellement réencodée, jamais annoncée Baseline sans conversion. Le profil 1080p réencodé utilise le niveau 4.0 ; le 720p utilise 3.1. L'affichage réussi ici ne garantit pas leur acceptation par tous les autres navigateurs.

Chaque essai lit la vidéo pendant environ 35 secondes après le premier rappel de présentation. Les 5 premières secondes sont exclues du régime établi ; la fenêtre principale couvre premier rappel + 5 s jusqu'à premier rappel + 35 s, bornée par la fin effective. Les modes sont alternés entre répétitions. Une mesure au repos est prise entre essais ; elle est conservée pour contrôle, sans soustraction arbitraire aux charges brutes.

### Définition des indicateurs

Le navigateur synchronise performance.now() avec l'horloge serveur par 12 échanges HTTP ; l'échange au plus petit aller-retour sert à l'offset. Le marqueur est lu dans une mince bande de canvas, au plus quatre fois par seconde. requestVideoFrameCallback fournit le compteur de présentation et expectedDisplayTime. Cette dernière valeur reste une estimation navigateur de présentation, pas une mesure des photons de l'écran.

La **latence idéale** vaut heure de présentation estimée − (début de source + identifiant / 30). Elle inclut le retard éventuel d'émission lorsque le CPU ou la contre-pression ralentit le banc, et constitue la comparaison principale. La latence après émission réelle, également conservée, retire ce retard amont : seule, elle pourrait donner une impression trompeuse de rapidité sous saturation. Le retard d'émission est mesuré uniquement pendant la fenêtre utile, sans les délais de nettoyage après arrêt.

Première mire identifiée : temps depuis le lancement de la chaîne jusqu'au premier marqueur valide. C'est une borne haute du démarrage réel, à la précision de l'estimation de présentation, car le marqueur n'est lu qu'environ toutes les 250–300 ms (davantage sous saturation). Le premier rappel WebRTC peut être noir : il n'est pas présenté comme preuve d'une première image utile. Le délai du premier rappel est conservé séparément ; les fenêtres de régime établi restent ancrées sur ce rappel, sans modifier rétroactivement les fenêtres mesurées. P50/P95 : quantiles des observations de latence de chaque essai. Dérive : médiane des 5 dernières secondes de mesure moins celle des 5 premières ; une valeur positive signifie un retard qui augmente. FPS : variation du compteur presentedFrames divisée par le temps observé, pas simple comptage des rappels JavaScript. Images abandonnées : variation droppedVideoFrames / variation totalVideoFrames pendant les observations de la fenêtre. Une absence de rappel JavaScript ne prouve pas à elle seule un gel si le compteur de présentation a avancé.

CPU : GetSystemTimes et GetProcessTimes, échantillonnés approximativement chaque seconde ; moyennes pondérées par le chevauchement temporel avec la fenêtre. 100 % représente toute la VM, ici son unique CPU. Le serveur comprend le processus Python de mesure/relais et ses enfants FFmpeg. La colonne FFmpeg est donc un sous-ensemble du serveur : ne pas les additionner. Le navigateur regroupe son arbre de processus dédié. Le CPU global comprend aussi Windows et les autres tâches de la VM.

Mémoire : sommes par arbre de processus. « privée » désigne les octets privés engagés, pas nécessairement tous résidents. « RSS/working set » désigne les pages résidentes ; les pages partagées peuvent être comptées plusieurs fois dans une somme de processus. Toutes les tailles sont en Mio (2²⁰ octets).

Les tableaux agrégés donnent **médiane [minimum–maximum] entre répétitions**. Les P95 affichés sont la médiane des P95 individuels, pas un P95 calculé en mélangeant toutes les images. Trois répétitions décrivent la variabilité observée ; elles ne constituent pas un intervalle de confiance.

## Résultats vidéo agrégés

| Mode | n | Première mire identifiée, borne haute (s) | Latence idéale P50 (s) | P95 (s) | Dérive (s) | FPS présentés | Images abandonnées (%) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| MSE copie 1080p30 | 3 | 1,86 [1,84–1,87] | 1,71 [1,70–1,76] | 1,72 [1,71–1,78] | 0,02 [0,00–0,02] | 29,9 [29,9–30,0] | 0,34 [0,00–0,78] |
| WebRTC natif 1080p30 | 3 | 1,68 [1,45–1,70] | 1,80 [1,77–1,89] | 2,09 [1,97–2,15] | 0,53 [0,52–0,72] | 28,8 [28,3–29,1] | 1,82 [0,80–3,22] |
| WebRTC réencodé 1080p30 | 3 | 4,13 [3,62–4,24] | 3,99 [3,93–4,53] | 4,37 [4,14–4,93] | 0,50 [0,30–0,73] | 23,1 [21,4–23,2] | 21,00 [19,34–22,02] |
| WebRTC réencodé 720p30 | 3 | 4,23 [4,15–4,39] | 4,07 [3,45–4,43] | 4,23 [3,80–4,72] | 0,35 [0,32–0,60] | 24,8 [18,7–25,4] | 15,00 [13,94–35,16] |
| WebRTC réencodé 720p15 | 3 | 3,99 [3,95–4,25] | 4,05 [4,01–4,43] | 4,28 [4,27–4,75] | 0,49 [0,41–0,65] | 14,7 [14,6–14,7] | 0,23 [0,23–0,46] |

La comparaison à définition/cadence cible identiques est MSE 1080p30 contre WebRTC réencodé 1080p30. Les variantes 720p et 15 FPS sont des compromis de définition ou de fluidité, pas un gain à qualité égale. Le contenu réencodé subit en outre une compression supplémentaire ; aucune mesure perceptuelle de qualité n'a été réalisée.

## Charge CPU agrégée (%)

Les quatre premières colonnes de charge utilisent la moyenne temporelle de chaque essai. Le P95 et le pic portent sur les échantillons CPU de la fenêtre.

| Mode | Global moyen | Serveur moyen (FFmpeg inclus) | Navigateur moyen | FFmpeg seul moyen | Global P95 | Global pic |
| --- | --- | --- | --- | --- | --- | --- |
| MSE copie 1080p30 | 53,6 [46,9–53,9] | 2,1 [1,9–2,6] | 49,6 [43,1–50,4] | 0,6 [0,5–0,7] | 73,1 [58,5–78,8] | 76,9 [61,2–87,3] |
| WebRTC natif 1080p30 | 62,2 [61,8–62,9] | 8,4 [7,4–8,7] | 52,2 [51,6–54,3] | 0,0 [0,0–0,0] | 72,3 [71,9–76,6] | 75,0 [75,0–79,4] |
| WebRTC réencodé 1080p30 | 93,6 [93,4–95,1] | 58,4 [57,1–59,3] | 34,5 [33,6–35,2] | 49,9 [49,8–50,7] | 100,0 [100,0–100,0] | 100,0 [100,0–100,0] |
| WebRTC réencodé 720p30 | 83,0 [78,1–95,5] | 52,1 [47,9–65,8] | 29,0 [28,2–30,3] | 42,6 [39,9–55,8] | 92,1 [90,8–100,0] | 96,9 [92,2–100,0] |
| WebRTC réencodé 720p15 | 59,5 [57,2–61,6] | 38,7 [36,0–38,8] | 20,3 [19,8–21,5] | 31,4 [29,1–31,4] | 73,8 [68,8–75,4] | 81,7 [68,8–84,1] |

## Mémoire agrégée (Mio)

Valeurs moyennes temporelles, puis médiane [minimum–maximum] des répétitions. Le serveur inclut FFmpeg ; les RSS ne sont pas des mémoires privées.

| Mode | Serveur privé | Navigateur privé | Serveur RSS/working set, somme | Navigateur RSS/working set, somme |
| --- | --- | --- | --- | --- |
| MSE copie 1080p30 | 109,1 [98,6–110,7] | 1360,3 [1349,0–1455,5] | 100,8 [86,5–102,4] | 492,5 [491,2–499,2] |
| WebRTC natif 1080p30 | 93,4 [91,6–94,9] | 1345,7 [1329,2–1429,7] | 88,8 [87,0–90,7] | 477,2 [463,0–490,1] |
| WebRTC réencodé 1080p30 | 163,1 [158,7–168,2] | 1339,1 [1336,9–1449,9] | 135,5 [130,8–140,5] | 492,5 [472,8–516,8] |
| WebRTC réencodé 720p30 | 139,9 [139,9–143,9] | 1333,1 [1319,8–1429,6] | 123,6 [123,3–127,8] | 438,1 [421,5–458,5] |
| WebRTC réencodé 720p15 | 137,4 [135,8–145,3] | 1324,7 [1322,8–1328,6] | 120,6 [119,0–129,4] | 434,9 [415,4–435,9] |

## Détail par essai

Les lignes invalides restent visibles pour la traçabilité, mais sont exclues de tous les agrégats.

| Essai | Statut | Fenêtre (s) | Première mire identifiée, borne haute (s) | P50 idéal (s) | P95 idéal (s) | Dérive (s) | FPS | Abandonnées (%) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| clean1/r1/baseline720_15 | valide | 30,0 | 3,99 | 4,05 | 4,27 | 0,49 | 14,7 | 0,23 |
| clean1/r2/baseline720_15 | valide | 30,0 | 4,25 | 4,43 | 4,75 | 0,65 | 14,6 | 0,23 |
| clean1/r2/baseline720 | valide | 30,0 | 4,15 | 4,43 | 4,72 | 0,60 | 24,8 | 15,00 |
| clean1/r2/native | valide | 30,0 | 1,70 | 1,80 | 2,15 | 0,72 | 28,3 | 3,22 |
| clean1/r2/baseline1080 | valide | 30,0 | 3,62 | 3,99 | 4,37 | 0,73 | 21,4 | 22,02 |
| clean1/r2/mse | valide | 30,0 | 1,86 | 1,71 | 1,71 | 0,00 | 30,0 | 0,00 |
| clean1/r3/native | valide | 30,0 | 1,68 | 1,89 | 2,09 | 0,52 | 29,1 | 0,80 |
| clean1/r3/mse | valide | 30,0 | 1,87 | 1,70 | 1,72 | 0,02 | 29,9 | 0,34 |
| clean1/r3/baseline720 | valide | 30,0 | 4,23 | 3,45 | 3,80 | 0,35 | 18,7 | 35,16 |
| clean1/r3/baseline720_15 | valide | 30,0 | 3,95 | 4,01 | 4,28 | 0,41 | 14,7 | 0,46 |
| clean1/r3/baseline1080 | valide | 30,0 | 4,24 | 4,53 | 4,93 | 0,50 | 23,2 | 19,34 |
| extra1/r1/mse | valide | 30,0 | 1,84 | 1,76 | 1,78 | 0,02 | 29,9 | 0,78 |
| extra1/r1/baseline1080 | valide | 30,0 | 4,13 | 3,93 | 4,14 | 0,30 | 23,1 | 21,00 |
| extra1/r1/native | valide | 30,0 | 1,45 | 1,77 | 1,97 | 0,53 | 28,8 | 1,82 |
| extra1/r1/baseline720 | valide | 30,0 | 4,39 | 4,07 | 4,23 | 0,32 | 25,4 | 13,94 |

### Ressources par essai

| Essai | CPU global (%) | CPU serveur (%) | CPU navigateur (%) | CPU FFmpeg (%) | Privé serveur (Mio) | Privé navigateur (Mio) | RSS serveur (Mio) | RSS navigateur (Mio) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| clean1/r1/baseline720_15 | 57,2 | 36,0 | 20,3 | 29,1 | 135,8 | 1328,6 | 119,0 | 435,9 |
| clean1/r2/baseline720_15 | 59,5 | 38,7 | 19,8 | 31,4 | 137,4 | 1322,8 | 120,6 | 434,9 |
| clean1/r2/baseline720 | 83,0 | 52,1 | 30,3 | 42,6 | 139,9 | 1319,8 | 123,3 | 438,1 |
| clean1/r2/native | 61,8 | 8,7 | 51,6 | 0,0 | 93,4 | 1329,2 | 88,8 | 477,2 |
| clean1/r2/baseline1080 | 95,1 | 59,3 | 35,2 | 49,8 | 163,1 | 1336,9 | 135,5 | 492,5 |
| clean1/r2/mse | 46,9 | 2,1 | 43,1 | 0,5 | 109,1 | 1349,0 | 100,8 | 491,2 |
| clean1/r3/native | 62,2 | 8,4 | 52,2 | 0,0 | 94,9 | 1345,7 | 90,7 | 463,0 |
| clean1/r3/mse | 53,6 | 2,6 | 49,6 | 0,7 | 110,7 | 1360,3 | 102,4 | 492,5 |
| clean1/r3/baseline720 | 95,5 | 65,8 | 28,2 | 55,8 | 143,9 | 1333,1 | 127,8 | 421,5 |
| clean1/r3/baseline720_15 | 61,6 | 38,8 | 21,5 | 31,4 | 145,3 | 1324,7 | 129,4 | 415,4 |
| clean1/r3/baseline1080 | 93,4 | 58,4 | 33,6 | 50,7 | 168,2 | 1339,1 | 140,5 | 472,8 |
| extra1/r1/mse | 53,9 | 1,9 | 50,4 | 0,6 | 98,6 | 1455,5 | 86,5 | 499,2 |
| extra1/r1/baseline1080 | 93,6 | 57,1 | 34,5 | 49,9 | 158,7 | 1449,9 | 130,8 | 516,8 |
| extra1/r1/native | 62,9 | 7,4 | 54,3 | 0,0 | 91,6 | 1429,7 | 87,0 | 490,1 |
| extra1/r1/baseline720 | 78,1 | 47,9 | 29,0 | 39,9 | 139,9 | 1429,6 | 123,6 | 458,5 |

### Contrôle du démarrage

| Essai | Premier rappel (s) | Première mire identifiée, borne haute (s) | Marqueur valide au premier rappel | Intervalle maximal initial du marqueur (ms) |
| --- | --- | --- | --- | --- |
| clean1/r1/baseline720_15 | 3,99 | 3,99 | oui | 334,9 |
| clean1/r2/baseline720_15 | 4,25 | 4,25 | oui | 314,7 |
| clean1/r2/baseline720 | 4,14 | 4,15 | oui | 283,4 |
| clean1/r2/native | 1,44 | 1,70 | non | 283,3 |
| clean1/r2/baseline1080 | 3,62 | 3,62 | oui | 283,4 |
| clean1/r2/mse | 1,85 | 1,86 | oui | 266,6 |
| clean1/r3/native | 1,42 | 1,68 | non | 266,6 |
| clean1/r3/mse | 1,82 | 1,87 | oui | 266,7 |
| clean1/r3/baseline720 | 3,95 | 4,23 | non | 283,4 |
| clean1/r3/baseline720_15 | 3,96 | 3,95 | oui | 270,9 |
| clean1/r3/baseline1080 | 4,23 | 4,24 | oui | 300,0 |
| extra1/r1/mse | 1,83 | 1,84 | oui | 292,2 |
| extra1/r1/baseline1080 | 4,14 | 4,13 | oui | 327,5 |
| extra1/r1/native | 1,45 | 1,45 | oui | 268,5 |
| extra1/r1/baseline720 | 4,09 | 4,39 | non | 293,2 |

### Contrôles de mesure par essai

| Essai | Marqueurs valides (%) | Couverture CPU (%) | RTT minimal (ms) | Retard émission P95 (ms) | P50 après émission réelle (s) | Résolution observée |
| --- | --- | --- | --- | --- | --- | --- |
| clean1/r1/baseline720_15 | 100,0 | 99,7 | 4,00 | 12,3 | 4,05 | 1280 × 720 |
| clean1/r2/baseline720_15 | 100,0 | 99,5 | 4,10 | 12,6 | 4,43 | 1280 × 720 |
| clean1/r2/baseline720 | 100,0 | 100,0 | 4,10 | 14,6 | 4,43 | 1280 × 720 |
| clean1/r2/native | 100,0 | 99,0 | 4,10 | 17,1 | 1,80 | 1920 × 1080 |
| clean1/r2/baseline1080 | 100,0 | 98,7 | 4,10 | 89,6 | 4,00 | 1920 × 1080 |
| clean1/r2/mse | 100,0 | 97,6 | 4,10 | 22,4 | 1,70 | 1920 × 1080 |
| clean1/r3/native | 100,0 | 98,5 | 4,10 | 18,9 | 1,89 | 1920 × 1080 |
| clean1/r3/mse | 100,0 | 98,9 | 4,10 | 40,6 | 1,70 | 1920 × 1080 |
| clean1/r3/baseline720 | 100,0 | 99,8 | 6,90 | 2264,8 | 3,41 | 1280 × 720 |
| clean1/r3/baseline720_15 | 100,0 | 100,0 | 4,00 | 12,1 | 4,02 | 1280 × 720 |
| clean1/r3/baseline1080 | 100,0 | 100,0 | 4,20 | 22,8 | 4,53 | 1920 × 1080 |
| extra1/r1/mse | 100,0 | 97,9 | 4,20 | 34,9 | 1,76 | 1920 × 1080 |
| extra1/r1/baseline1080 | 100,0 | 99,6 | 6,30 | 43,2 | 3,93 | 1920 × 1080 |
| extra1/r1/native | 100,0 | 98,3 | 4,00 | 17,7 | 1,77 | 1920 × 1080 |
| extra1/r1/baseline720 | 100,0 | 99,4 | 4,10 | 23,8 | 4,08 | 1280 × 720 |

Un retard d'émission P95 supérieur à 100 ms est signalé comme pression sur le banc, sans écarter arbitrairement une vraie saturation. L'erreur d'offset d'horloge liée à l'échange retenu est bornée approximativement par la moitié du RTT ; elle n'inclut pas l'incertitude de présentation écran.

### Débits et gels : compteurs sur la durée totale de connexion

Ces compteurs incluent le démarrage et ne portent pas seulement sur les 30 secondes de régime établi. Le débit RTC utilise bytesReceived vidéo, hors en-têtes réseau ; le débit MSE utilise les octets de corps MP4 envoyés. Les bases ne sont donc pas strictement identiques. Le compteur serveur MSE lu à l'arrêt peut inclure quelques octets de nettoyage.

| Essai | Débit RTC reçu (Mbit/s) | Débit MSE envoyé (Mbit/s) | Gels RTC | Durée gels RTC (s) | Plus grand intervalle entre observations (ms) |
| --- | --- | --- | --- | --- | --- |
| clean1/r1/baseline720_15 | 1,78 | — | 0 | 0,000 | 133,3 |
| clean1/r2/baseline720_15 | 1,76 | — | 0 | 0,000 | 150,0 |
| clean1/r2/baseline720 | 1,75 | — | 0 | 0,000 | 166,7 |
| clean1/r2/native | 1,90 | — | 0 | 0,000 | 99,9 |
| clean1/r2/baseline1080 | 1,78 | — | 3 | 0,682 | 266,7 |
| clean1/r2/mse | — | 1,97 | — | — | 100,0 |
| clean1/r3/native | 1,90 | — | 0 | 0,000 | 83,4 |
| clean1/r3/mse | — | 1,97 | — | — | 166,6 |
| clean1/r3/baseline720 | 1,78 | — | 12 | 3,538 | 566,7 |
| clean1/r3/baseline720_15 | 1,78 | — | 1 | 0,259 | 233,3 |
| clean1/r3/baseline1080 | 1,75 | — | 1 | 0,339 | 333,3 |
| extra1/r1/mse | — | 1,97 | — | — | 133,3 |
| extra1/r1/baseline1080 | 1,78 | — | 4 | 1,182 | 416,7 |
| extra1/r1/native | 1,91 | — | 0 | 0,000 | 83,4 |
| extra1/r1/baseline720 | 1,78 | — | 4 | 1,037 | 383,3 |

## Réserves et traçabilité

- `clean1/r3/baseline720` : retard d'émission P95 > 100 ms ; utiliser la latence idéale pour comparer, la latence après émission seule masque ce retard.

Sélection fixée avant le rapport : `extra1` remplace intégralement les quatre essais initiaux de `full2`, pour comparer avec une version de traitement et une procédure de nettoyage homogènes. `clean1` apporte les onze essais de complément. Dès que `extra1` existe, même partiel, aucun résultat `full2` n'est réinjecté pour remplir les cases. Les autres séries `full2`, `full1` et les pilotes restent écartées à cause des problèmes de nettoyage/processus résiduels ou d'interactions de diagnostic, pas en fonction du classement obtenu. Les éventuels essais GOP60 constituent une expérience séparée et ne sont pas agrégés ici.

Versions : `clean1` utilise le traitement WebRTC de `e706c4f`. La série de remplacement `extra1` charge une copie isolée de `blink_webrtc.py` et `blink_ts_demux.py` extraite de ce même commit et vérifiée identique, pour éviter les modifications concurrentes du dépôt. Son événement `loaded_source` trace le chemin chargé, la référence Git et l'empreinte SHA-256.

| Journal | Présent | Fin de journal observée | Essais sélectionnés | Essais exclus par sélection |
| --- | --- | --- | --- | --- |
| benchmark-full2.jsonl | oui | oui | 0 | 15 |
| benchmark-clean1.jsonl | oui | oui | 11 | 0 |
| benchmark-extra1.jsonl | oui | oui | 4 | 0 |

| Journal | Référence source tracée | SHA-256 de blink_webrtc.py chargé |
| --- | --- | --- |
| benchmark-clean1.jsonl | non journalisée ; voir notes de version | — |
| benchmark-extra1.jsonl | e706c4f | fd0522200cb0ca44d46133fe8d5c6a9dac31f5d5fca3117b72fd830bf629a146 |

La mesure n'inclut pas une analyse perceptuelle de la compression, un réseau dégradé ou plusieurs directs simultanés. Les compteurs de gels RTC et d'images abandonnées par le lecteur correspondent à des étages différents : ne pas les additionner. Le coût de l'instrumentation est inclus dans les processus mesurés. Le bloc de réencodage reste expérimental et ces essais n'établissent pas sa compatibilité Firefox.

## Reproduction du rapport

Lancer depuis la racine du dépôt ; la commande lit les journaux et écrit le Markdown sur la sortie standard uniquement :

```powershell
rtk proxy python build-win7/bench_report.py
```

Scripts du banc : `bench_fixture.py`, `bench_vm.py`, `bench_player.js`, `bench_winmetrics.py`, `bench_summarize.py` et `bench_report.py`, sous `build-win7/`. Les données brutes sélectionnées sont `benchmark-extra1.jsonl` et `benchmark-clean1.jsonl`. Aucune modification TLS Blink n'est nécessaire pour ce banc synthétique.

## Annexe séparée : sensibilité à un GOP source de 60 images

Contrôle complémentaire avec un GOP source de 60 images, soit 2 secondes à 30 FPS, plus proche de l'intervalle d'images-clés observé dans des flux Blink. Ces essais ne sont jamais mélangés aux agrégats principaux GOP30. Ils restent synthétiques, et ne constituent pas un test de caméra réelle. Le réencodeur Baseline conserve son propre GOP de 1 seconde : c'est le GOP de la source High et du MSE copié qui passe à 2 secondes.

Essais de contrôle disponibles : 3 ; exploitables : 3. Une seule répétition par mode ne permet pas d'estimer la dispersion ; ce contrôle sert à vérifier la sensibilité au découpage en fragments.

| Essai | Statut | Première mire identifiée, borne haute (s) | P50 idéal (s) | P95 idéal (s) | FPS | Abandonnées (%) | CPU global moyen (%) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| benchmark-gop2final1/r1/mse | valide | 2,30 | 2,34 | 2,34 | 29,8 | 0,67 | 55,8 |
| benchmark-gop2final1/r1/native | valide | 1,89 | 1,51 | 3,78 | 26,2 | 5,35 | 71,0 |
| benchmark-gop2final1/r1/baseline720_15 | valide | 4,10 | 4,04 | 4,44 | 14,2 | 2,76 | 63,4 |

Les mêmes définitions de fenêtre, d'horloge et de validité s'appliquent à cette annexe.

## Artefacts conservés

- [Journal principal : clean1](build-win7/benchmark-clean1.jsonl)
- [Répétition de contrôle homogène : extra1](build-win7/benchmark-extra1.jsonl)
- [Contrôle séparé, GOP 2 secondes](build-win7/benchmark-gop2final1.jsonl)
- [Calcul du rapport](build-win7/bench_report.py)
- [Calcul des indicateurs](build-win7/bench_summarize.py)
- [Serveur du banc](build-win7/bench_vm.py)
- [Lecteur et marqueur navigateur](build-win7/bench_player.js)
- [Contrôle final des processus](build-win7/benchmark-cleanup-verification.txt)

Les journaux et le banc restent locaux dans `build-win7/`, répertoire ignoré par Git. Ne pas supprimer ce répertoire si ces données doivent être conservées. Ce rapport est un nouveau fichier indépendant ; aucune caméra n'a été réveillée pour ces essais, aucun compte ni réglage Blink n'a été changé, et aucune modification de production n'a été faite par ce benchmark.
