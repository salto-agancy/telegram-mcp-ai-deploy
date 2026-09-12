# Backup

`scripts/backup.sh` streams both named volumes directly into an AES-256 encrypted archive;
there is no plaintext intermediate. Supply a strong passphrase through the hidden prompt and
store it separately. Backups are ignored by Git.

Restore in a maintenance window: stop the stack, decrypt into the matching named volumes,
verify ownership, start the previous image, then run the full healthcheck. A Telegram session
backup is a high-value login credential; revoke the Telegram session from official Telegram
settings if an archive or passphrase may be compromised.
