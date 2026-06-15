# Secrets

The Airflow secrets are kept out of git. `airflow.cfg` is committed with its
secret fields blank, and the real values live in a GPG-encrypted file in your
home directory that gets loaded into the environment at launch.

This only concerns you if you *run* an Airflow instance. If you just log into
someone else's running instance through the web UI, you need none of this — use
your EECS LDAP credentials and you're done. (The one exception is running
`hammer-pd-store` against the cache DB directly, which needs your own Postgres
password via `HAMMER_PG_PASSWORD` or a `~/.pgpass` entry.)

## How it works

The five secret fields in `airflow.cfg` — `sql_alchemy_conn`, `fernet_key`,
`secret_key`, `jwt_secret`, and `internal_api_secret_key` — are blank in the
committed file. The real values are symmetric-encrypted under your passphrase in
`~/.config/sledgehammer/airflow-secrets.env.gpg`.

`scripts/airflow-standalone-ldap.py` decrypts that at startup, in memory, and
puts the values in the environment, where Airflow reads them in place of the
blank cfg. You type the passphrase once per launch; gpg-agent caches it for about
20 minutes after that, so back-to-back restarts won't re-prompt.

## First-time setup

### Migrating an airflow.cfg that still has the real values

Let the script pull the secrets out, encrypt them, and blank the cfg:

    cd <your hammer checkout>
    source ./venv.sh && export PATH=$(pwd)/.venv/bin:$PATH
    ./scripts/sledge-secrets-init.py        # pick a passphrase; answer 'y' to blank the cfg
    ./scripts/airflow-standalone-ldap.py    # confirm it boots, then Ctrl-C
    git diff airflow.cfg                     # the secret lines should be blank

`airflow.cfg` is no longer gitignored but still holds secrets until the script
blanks it, so don't `git add` it until that diff shows blank values.

### A new operator on a fresh checkout

The committed `airflow.cfg` is already blank, so there's nothing to pull out.
Fill in your own values from the template and encrypt them — a separate instance
gets its own keys, don't reuse anyone else's.

    cp scripts/airflow-secrets.env.template /dev/shm/airflow-secrets.env
    # edit it: your sql_alchemy_conn, plus fresh keys --
    #   fernet:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    #   others:  python -c "import secrets; print(secrets.token_hex(32))"
    mkdir -p ~/.config/sledgehammer && chmod 700 ~/.config/sledgehammer
    gpg --symmetric --cipher-algo AES256 \
        -o ~/.config/sledgehammer/airflow-secrets.env.gpg /dev/shm/airflow-secrets.env
    chmod 600 ~/.config/sledgehammer/airflow-secrets.env.gpg
    shred -u /dev/shm/airflow-secrets.env

## Day to day

    source ./venv.sh && export PATH=$(pwd)/.venv/bin:$PATH
    ./scripts/airflow-standalone-ldap.py

Run it under tmux so an SSH drop doesn't orphan the workers.

## Rotating a secret

Decrypt into RAM, edit, re-encrypt:

    T=/dev/shm/secrets-$USER.env
    gpg --decrypt ~/.config/sledgehammer/airflow-secrets.env.gpg > "$T"
    $EDITOR "$T"
    gpg --symmetric --cipher-algo AES256 --yes \
        -o ~/.config/sledgehammer/airflow-secrets.env.gpg.new "$T"
    mv ~/.config/sledgehammer/airflow-secrets.env.gpg{.new,}
    shred -u "$T"

For the DB password, also `ALTER ROLE <you> WITH PASSWORD ...` in Postgres.
Changing `fernet_key` invalidates the connection passwords stored in the Airflow
database.

## If you forget the passphrase

There's no backup by design. Recovery means regenerating the secrets — most are
disposable; only `fernet_key` loses data (the connections stored in the Airflow
DB). The steps and the options for a real recovery path are in
[GPG_RECOVERY_IDEAS.md](GPG_RECOVERY_IDEAS.md).

## What's committed

In git: `airflow.cfg` with blank secrets, the scripts, and these docs. Never in
git (and gitignored): the `.gpg` file, any decrypted `.env`, and your passphrase.
