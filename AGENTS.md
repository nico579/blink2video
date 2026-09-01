# Instructions du dépôt

## Garde-fou critique : TLS du Live View

- Ne jamais modifier, remplacer ou monkey-patcher `blinkpy.livestream.BlinkLiveStream.auth` ni son contexte TLS.
- Le relais vidéo Live View de Blink présente un certificat auto-signé. `blinkpy` lui applique volontairement un contexte `CERT_NONE` limité à cette connexion. Le remplacement par `ssl.create_default_context()` a cassé tous les directs de blink2video 0.10.6 à 0.10.10 avec `CERTIFICATE_VERIFY_FAILED`, avant le premier octet vidéo.
- Cette exception concerne uniquement le relais vidéo Live View. Les appels ordinaires aux API Blink doivent conserver la validation TLS stricte fournie par `blink_auth.contexte_tls()`.
- Si une évolution future semble imposer de toucher à ce comportement, arrêter le travail et demander d’abord l’autorisation explicite de l’utilisateur. Exiger ensuite le test automatisé `test_livestream_relais_auto_signe_conserve_le_contexte_amont`, la suite complète et un essai sur caméra réelle démontrant HTTP 200, codec, `moov` et `moof`.
