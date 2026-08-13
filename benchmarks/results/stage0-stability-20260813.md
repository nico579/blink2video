# Contrôle de stabilité de l'étape 0

Date : 13 août 2026
Révision de production : `58f5566`
Profil : `quick`, sous-ensemble interne P-02 à P-07, P-09 et P-10

La campagne complète `quick-run-a` est invalide : les créations répétées de
processus de P-01 ont porté l'activité de `MsMpEng` à 100 % selon le constat de
l'utilisateur. Elle reste archivée uniquement comme preuve brute.

Trois campagnes sans P-01, P-08, P-11 et P-12 ont ensuite été réalisées. Elles
n'ont créé aucune rafale de processus, mais leur stabilité reste insuffisante.

## Comparaison A → B

| Scénario | A médiane | B médiane | Delta | Stable à 10 % |
|---|---:|---:|---:|---|
| P-02 / 100 | 3,415 ms | 2,677 ms | -21,62 % | non |
| P-02 / 1 000 | 17,435 ms | 15,233 ms | -12,63 % | non |
| P-02 / 5 000 | 315,410 ms | 73,521 ms | -76,69 % | non |
| P-03 / 5 000 | 140,296 ms | 78,877 ms | -43,78 % | non |
| P-04 / 1 000 | 87,811 ms | 85,466 ms | -2,67 % | oui |
| P-05 / 5 000 × 500 | 45,069 ms | 48,035 ms | +6,58 % | oui |
| P-06 / 600 | 2,187 ms | 1,424 ms | -34,89 % | non |
| P-07 / 1 Mio | 7,438 ms | 6,996 ms | -5,95 % | oui |
| P-07 / 8 Mio | 33,558 ms | 27,136 ms | -19,14 % | non |
| P-09 | 94,395 ms | 93,850 ms | -0,58 % | oui |
| P-10 / 8 écrivains | 5,279 ms | 5,729 ms | +8,53 % | oui |
| P-10 / série | 0,852 ms | 0,837 ms | -1,76 % | oui |

Résultat : 6 scénarios stables sur 12.

## Comparaison B → C

| Scénario | B médiane | C médiane | Delta | Stable à 10 % |
|---|---:|---:|---:|---|
| P-02 / 100 | 2,677 ms | 2,857 ms | +6,72 % | oui |
| P-02 / 1 000 | 15,233 ms | 29,975 ms | +96,78 % | non |
| P-02 / 5 000 | 73,521 ms | 94,454 ms | +28,47 % | non |
| P-03 / 5 000 | 78,877 ms | 112,758 ms | +42,95 % | non |
| P-04 / 1 000 | 85,466 ms | 94,525 ms | +10,60 % | non |
| P-05 / 5 000 × 500 | 48,035 ms | 55,529 ms | +15,60 % | non |
| P-06 / 600 | 1,424 ms | 1,865 ms | +30,99 % | non |
| P-07 / 1 Mio | 6,996 ms | 5,907 ms | -15,56 % | non |
| P-07 / 8 Mio | 27,136 ms | 24,382 ms | -10,15 % | non |
| P-09 | 93,850 ms | 93,937 ms | +0,09 % | oui |
| P-10 / 8 écrivains | 5,729 ms | 5,315 ms | -7,22 % | oui |
| P-10 / série | 0,837 ms | 0,764 ms | -8,69 % | oui |

Résultat : 4 scénarios stables sur 12. Le critère de sortie de l'étape 0 n'est
donc pas satisfait. Aucun gain de performance ne peut être revendiqué.

Les JSON A, B et C conservent tous les échantillons, p95, coefficients de
variation et intervalles de confiance. Une nouvelle campagne qualifiante devra
être conduite sur une machine au repos, sans désactiver l'antivirus, et avec un
protocole de démarrage accepté par celui-ci.
