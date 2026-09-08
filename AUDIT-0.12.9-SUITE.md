# Audit complémentaire de 0.12.9 — 7 septembre 2026

Base : `c3044f4`, version publiée 0.12.9. Les douze corrections précédentes
restent en place. Les cinq défauts ci-dessous ont été reproduits, puis
**corrigés pour 0.12.10**. Les descriptions des
reproductions et les numéros de ligne correspondent à la version avant correction.

## 1. P1 — Suppression d'un MP4 hors du dossier des directs

Sources : `serve.py:69`, `serve.py:3285`, `serve.py:3513`.

La validation `IDENTITY` accepte les composants `..`. La classification des
enregistrements directs vérifie seulement que `racine_direct / identity`
désigne un fichier. La suppression emploie ensuite ce chemin sans vérifier
que sa résolution reste sous la racine autorisée.

Reproduction : POST `/api/appliquer-selection` avec une identité contenant
`../` et le nom d'un MP4 témoin placé dans le parent du dossier des directs.
Le témoin est supprimé et la réponse annonce `ok: true`, `supprime`.
Le test utilise uniquement une sentinelle créée dans un répertoire temporaire.

Portée : la requête doit passer les contrôles d'hôte et de jeton de
l'application ; ce diagnostic ne démontre pas une attaque distante sans
authentification. La cible doit être un MP4 accessible au compte exécutant
l'application. Aucun appel Blink n'est nécessaire pour déclencher ce chemin.

Correction appliquée : validation stricte de chaque identité, résolution et
confinement des chemins avant toute mutation du lot. Les traversées et les
liens sortant de la racine sont refusés. Nouvelle vérification avant suppression ;
le nettoyage des dossiers vides ne supprime jamais la racine des directs.
Tests : `test_serve_direct_confinement.py` et
`test_serve_appliquer_selection_lock.py`.

## 2. P1 — Retour arrière de mise à jour non préservé après un échec de restauration

Source : `maj.py:677`, particulièrement les lignes 692 et 698–707.

Le remplacement intercepte les erreurs de restauration sans les distinguer
d'un retour arrière réussi. Le finaliseur réessaie alors la permutation ;
chaque essai peut supprimer ou remplacer les sauvegardes `.ancien`.

Reproduction sur fichiers factices : la copie des nouvelles bibliothèques
échoue ; la restauration de l'ancien exécutable est refusée une seule fois.
Après trois appels à `_permuter`, tous renvoient `False`, mais l'ancien
exécutable n'existe plus, ni en place ni en sauvegarde. L'installation
contient l'exécutable neuf avec les bibliothèques anciennes.

Ce scénario exige plusieurs erreurs d'E/S ; il n'est pas observé sur une
mise à jour réelle. Il démontre néanmoins que le message annonçant une
version précédente intacte n'est pas garanti dans ce chemin.

Correction appliquée : marqueur exclusif avant la permutation, retour arrière
en ordre inverse et retrait des éléments nouveaux partiellement copiés. Une
restauration incomplète interdit les nouveaux essais, la relance et le nettoyage
des sauvegardes/préparations. Un arrêt brutal conserve aussi ce garde-fou.
Un verrou interprocess commun au nettoyage et à la permutation ferme aussi
la course identifiée en relecture : un nettoyage ne peut plus valider l'absence
de marqueur puis effacer une sauvegarde créée par une permutation concurrente.
Cette réservation est attachée à l'installation, indépendamment de `BLINK_HOME`.
Une réparation explicite reste nécessaire après une restauration incomplète ;
le correctif préserve les sauvegardes mais ne prétend pas réparer toute panne d'E/S.
Tests : `test_maj_restauration.py`.

## 3. P2 — Échec des réglages après publication du nouveau stockage

Source : `serve.py:3598` à `serve.py:3612`.

La route publie le changement de stockage, puis écrit les nouveaux réglages.
Une erreur de cette seconde écriture renvoie HTTP 500 sans annuler le
pointeur ni redémarrer. Les modules déjà chargés conservent leurs chemins
initialisés dans l'ancien dossier, alors que `runtime.app_dir()` annonce
le nouveau.

Reproduction : vraie migration vers un dossier temporaire, puis
`PermissionError` simulée dans `ecrire_reglages`. Résultat : HTTP 500,
pointeur neuf présent, racine dynamique nouvelle, `serve.BASE_DIR` ancienne,
aucun redémarrage demandé. Le risque est un fonctionnement partagé entre
deux racines malgré une opération annoncée en échec.

Correction appliquée : copie de l'état et écriture des nouveaux réglages dans
la destination avant publication du pointeur. Une erreur de préparation
laisse donc l'ancienne racine active. Si le marqueur de configuration initiale
échoue ensuite, restauration du pointeur précédent. La priorité de `BLINK_HOME`
et le retour au dossier par défaut sont conservés.
Tests : `test_serve_redemarrage_fiable.py`.

## 4. P2 — Désactiver la suppression automatique ne suspend pas celle du lot courant

Sources : `blink_engine.py:389` / `:450` pour le cloud, `:791` / `:823` pour
le chemin USB.

L'ensemble des caméras autorisées est lu une seule fois avant le traitement
du lot. Une désactivation enregistrée pendant le transfert n'est donc pas
consultée avant les suppressions suivantes.

Reproduction USB et cloud : deux clips, désactivation réelle du fichier de
préférences pendant le premier transfert. Les deux appels de suppression
simulés ont ensuite lieu ; la préférence sur disque est vide au moment de
chacun d'eux. Les copies locales sont conservées : aucune perte de copie
locale n'est démontrée.

Nuance : la relecture au passage suivant est documentée dans les commentaires
du code. Le défaut est l'application différée d'un refus de suppression,
sans avertissement dans l'interface. Ce n'est pas la course entre deux clics
corrigée dans 0.12.9.

Correction appliquée : relecture de l'autorisation après chaque copie locale,
immédiatement avant chaque appel de suppression USB/cloud. Aucun verrou de
réglages n'est gardé pendant l'appel réseau. Une requête déjà envoyée peut
encore aboutir ; la désactivation protège les suppressions suivantes.
Tests : `test_suppression_auto_pendant_lot.py`.

## 5. P2 — La surveillance masque les caméras homonymes

Sources : `watch.py:71`, `watch.py:83`, `watch.py:123`.

L'état brut puis l'état surveillé sont indexés uniquement par nom. Une caméra
homonyme d'un autre réseau écrase la précédente. La date du dernier clip est
elle aussi regroupée par nom.

Reproduction : deux caméras « Jardin », réseaux 111 et 222. La première est
hors ligne avec une batterie faible ; la seconde est en ligne, batterie OK.
L'état produit contient une seule caméra, saine, et `compare()` ne signale
aucune alerte, même au premier passage.

Correction appliquée : identités réseau/appareil dans l'état, les comparaisons
et les dates d'activité, récupération des appareils depuis l'état brut même
si blinkpy a écrasé un homonyme. Les noms uniques restent inchangés ; les
homonymes affichent réseau et appareil. Migration des sourdines et adaptation
du réglage de suppression automatique ; les anciens clips ambigus ne sont
jamais attribués arbitrairement à une caméra pour autoriser leur suppression.
Tests : `test_watch_identity.py`.

## Points antérieurs toujours ouverts, distincts des cinq nouveaux défauts

- Autostart macOS : un chemin contenant `&` donne un plist XML invalide ;
  deux compositions différentes reçoivent aussi le même `Label`. Ces points
  étaient déjà consignés dans l'audit du 13 août, section 28.59. XML et
  collision confirmés sur le contenu généré, pas par un lancement réel macOS.
- Robustesse WebRTC : aucun plafond du tampon élémentaire en l'absence de
  séparateur H.264. Avec 10 000, 20 000 puis 30 000 paquets TS synthétiques
  invalides, le tampon retient respectivement 1 840 000, 3 680 000 et
  5 520 000 octets. Croissance confirmée, pas d'épuisement mémoire ni de
  panne caméra réelle observés.

## Reproductions et limites

Diagnostics locaux sous `build-win7` (ignorés par Git) :

- `audit-restants-0129.py` : retour arrière, autostart et tampon H.264.
- `audit-web-input-20260907.py` : suppression hors dossier et réglages.
- `audit-download-watch-20260907.py` : suppression différée et homonymes.

Les scripts de diagnostic ci-dessus décrivent le comportement défectueux
avant correction ; les tests de non-régression versionnés vérifient le correctif.

## Validation finale — 8 septembre 2026

- `python -m unittest discover -q` : 597 tests recensés sous Python 3.12,
  596 réussis et 1 ignoré ; mêmes résultats sous Python 3.8.10.
- Le test ignoré exige la création d'un lien symbolique natif, non autorisée
  sur cet hôte. Un test distinct simule une résolution hors racine et réussit.
- `python tests.py` : intégration complète réussie sous Python 3.12 et 3.8.10
  (processus temporaires, serveur de test, assemblage FFmpeg et mise à jour factice).
- `python docs.py --check` et `git diff --check` : réussis.
- 44 tests supplémentaires par rapport aux 553 tests de la version 0.12.9,
  dont 14 pour la restauration et la réservation de l'installation.

Résultats reproduits sous Python 3.12 et 3.8.10 sur l'hôte Windows. Aucun
processus utilisateur arrêté, aucune caméra réveillée, aucune suppression
distante ni modification TLS. Les suppressions locales de diagnostic
concernent uniquement des fichiers factices créés pour ces tests. La validation
précède la publication via `deploy.py`. Audit ciblé, pas une preuve d'absence
d'autres défauts.
