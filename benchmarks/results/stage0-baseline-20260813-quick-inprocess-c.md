# Campagne interne provisoire C — étape 0

> Campagne conservée, mais non qualifiante : le seuil de stabilité inférieur à
> 10 % entre deux campagnes successives n'a pas été atteint.

Date : 2026-08-13T17:01:40+02:00
Profil : `quick`
Révision de référence : `58f55660cf535d72e9a303b339df832e9ce437d5`
Graine : `20260813`

| ID | Statut | Mode | Médiane principale | Exactitude |
|---|---|---|---:|---|
| P-01 | not_run | excluded_by_operator | — | n/a |
| P-02 | measured | real_json_io | 2.857 ms | oui |
| P-03 | measured | real_json_io | 112.758 ms | oui |
| P-04 | measured | real_state_and_files | 94.525 ms | non |
| P-05 | measured | real_matching_synthetic_dataset | 55.529 ms | oui |
| P-06 | measured | real_parser_fake_blink_no_network | 1.865 ms | oui |
| P-07 | measured | real_loopback_http | 5.907 ms | oui |
| P-08 | not_run | excluded_by_operator | — | n/a |
| P-09 | measured | real_orchestrator_fake_io | 93.937 ms | oui |
| P-10 | measured | real_file_io_fake_tokens | 0.764 ms | oui |
| P-11 | not_run | excluded_by_operator | — | n/a |
| P-12 | not_run | excluded_or_requires_p01 | — | n/a |

Les valeurs détaillées, échantillons, p95, coefficients de variation et
intervalles de confiance bootstrap à 95 % se trouvent dans le JSON brut.
Une exactitude « non » décrit la baseline défectueuse ; elle invaliderait
toute revendication de gain pour une version candidate.

P-12 est volontairement partiel : les branches d'onboarding demandées
n'existent pas encore et sont marquées indisponibles, jamais mesurées à zéro.

## Écarts et sécurité

- P-07 est borné à 8 Mio : le flux 1 Gio est réservé au profil full; la machine de référence ne disposait que d'environ 3,4 Gio libres.
- P-02 à P-05 utilisent les volumes quick consignés dans chaque scénario; aucune extrapolation vers 100 000 entrées n'est revendiquée.
