# Keeping a personal deployment

Keep the reusable application public and the installation private. Do not fork
private IP addresses, provider-specific fixes, or credentials into tracked
source files.

For a personal installation:

1. Clone the public repository.
2. Keep `.env` untracked for passwords, feed tokens, public URLs, and optional
   integration endpoints.
3. Copy `deploy/private/compose.override.example.yaml` to
   `compose.override.yaml`. Docker Compose loads it automatically, and Git
   ignores it.
4. Put long-lived SQLite and export paths in the private environment file.
5. Export personal organization as an encrypted/private backup. Never commit
   the database or JSON backup.

If personal behavior needs code, first ask whether it can be a neutral setting.
Features useful to everyone should go upstream with disabled-by-default
configuration. Truly household-specific behavior belongs in a private branch or
private repository that regularly merges tagged public releases.

The production instance and a test instance must use different SQLite paths and
ports. Test a tagged release against a copied database, stop the test instance,
then upgrade production. Never mount the same database through two paths or run
two writers against it.
