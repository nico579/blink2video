# Campagne de performance invalide — étape 0

> **Ne pas utiliser comme baseline.** Pendant P-01, l'utilisateur a constaté
> que `MsMpEng` occupait 100 % des ressources à la suite des créations répétées
> de processus. Toute la campagne est conservée comme preuve brute, mais ses
> temps, dispersions et ressources sont contaminés par l'antivirus et ne
> permettent aucune comparaison avant/après.

Date : 2026-08-13T16:47:31+02:00
Profil : `quick`
Révision de référence : `58f55660cf535d72e9a303b339df832e9ce437d5`
Graine : `20260813`

| ID | Statut | Mode | Médiane principale | Exactitude |
|---|---|---|---:|---|
| P-01 | measured | real_subprocess | 3615.606 ms | oui |
| P-02 | measured | real_json_io | 4.759 ms | oui |
| P-03 | measured | real_json_io | 399.702 ms | oui |
| P-04 | measured | real_state_and_files | 123.462 ms | non |
| P-05 | measured | real_matching_synthetic_dataset | 236.635 ms | oui |
| P-06 | measured | real_parser_fake_blink_no_network | 5.261 ms | oui |
| P-07 | measured | real_loopback_http | 15.145 ms | oui |
| P-08 | measured | real_multiprocess_forced_interleaving | 5443.899 ms | non |
| P-09 | measured | real_orchestrator_fake_io | 84.373 ms | oui |
| P-10 | measured | real_file_io_fake_tokens | 3.163 ms | non |
| P-11 | measured | real_local_process_tree | 1046.836 ms | oui |
| P-12 | partial | current_cli_characterization | 3615.606 ms | oui |

Les valeurs détaillées, échantillons, p95, coefficients de variation et
intervalles de confiance bootstrap à 95 % se trouvent dans le JSON brut.
Une exactitude « non » décrit la baseline défectueuse ; elle invaliderait
toute revendication de gain pour une version candidate.

P-12 est volontairement partiel : les branches d'onboarding demandées
n'existent pas encore et sont marquées indisponibles, jamais mesurées à zéro.

## Écarts et sécurité

- P-07 est borné à 8 Mio : le flux 1 Gio est réservé au profil full; la machine de référence ne disposait que d'environ 3,4 Gio libres.
- P-02 à P-05 utilisent les volumes quick consignés dans chaque scénario; aucune extrapolation vers 100 000 entrées n'est revendiquée.
