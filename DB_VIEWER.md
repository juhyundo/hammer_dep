# Browsing the cache DB with DBeaver

The cache lives in Postgres (`hammer_poc.pd_blobs`, `pd_artifacts`,
`master_databases`, `user_workspaces`). For most day-to-day questions ("what's
in the cache?", "who ran this?", "how big is the GCD blob?") the CLI is
enough — `hammer-pd-store list`, `blob-list`, `master-list`. When you want a
GUI for ad-hoc browsing, sorting, or one-off SQL, DBeaver is what we
recommend.

DBeaver Community is free, native, and has SSH tunnel + jump host support
built into the connection settings — so you don't have to manage a separate
`ssh -L` window.

## Install

DBeaver Community Edition — one-click installers per OS:
<https://dbeaver.io/download/>

Pick "Community Edition." It's GPL, no license, no nag.

## Set up the connection

### Step 1: new Postgres connection

Database → New Database Connection → PostgreSQL → Next.

Leave the **Main** tab open; come back to it after the SSH tunnel is set up.

### Step 2: SSH tunnel (only needed if your Postgres isn't directly reachable)

In the same connection dialog, click the **SSH** tab on the left.

- Check **Use SSH Tunnel**
- **Host**: the SSH host that *can* reach your Postgres (see "Connection
  examples" below)
- **Port**: `22`
- **User**: your username on that host
- **Authentication**: usually "Public Key" with your `~/.ssh/id_rsa` (or
  whatever key you use); "Password" works too

If you also need to go through a **jump host** (BWRC users, this is you):

- Check **Use Jump Server**
- Fill in the same fields for the jump host (host, port 22, user, auth)

DBeaver opens both hops automatically when you connect. No `ssh -L` window
to leave open.

### Step 3: Postgres connection settings

Back on the **Main** tab:

- **Host**: the Postgres hostname *as seen from the SSH endpoint* (i.e. the
  real DB hostname, not `localhost` — DBeaver tunnels transparently)
- **Port**: usually `5432` or `5433`
- **Database**: e.g. `sledgehammer_studio`
- **Username**: your Postgres role
- **Password**: your Postgres password (DBeaver can save it)

Click **Test Connection**. If it succeeds, **Finish**. The connection shows
up in the left tree.

### Step 4: browse

Expand the connection → Databases → your DB → Schemas → `hammer_poc` →
Tables. Double-click a table; the **Data** tab shows the rows. Click a
column header to sort. Right-click a cell → "Filter by value" to filter
inline.

## Connection examples

### BWRC team deployment

If you're on the team and want to view the shared cache on `barney`:

**SSH tab:**
- Use SSH Tunnel: ✓
- Host: `bwrcix-3.eecs.berkeley.edu`, Port: `22`, User: your EECS UID
- Authentication: Public Key (your usual SSH key)
- Use Jump Server: ✓
- Jump Host: `bwrcrdsl-1.eecs.berkeley.edu`, Port: `22`, User: your EECS UID

**Main tab:**
- Host: `barney.eecs.berkeley.edu`
- Port: `5433`
- Database: `sledgehammer_studio`
- Username: your EECS UID (must be in `sledgehammer_users` group — ask an
  admin to run `hammer-pd-store grant your_uid` if you haven't been added)
- Password: your EECS password

### Your own Postgres (no SSH tunnel)

If Postgres runs on the same machine as DBeaver, or anywhere directly
reachable on the network:

**SSH tab:** leave **Use SSH Tunnel** unchecked.

**Main tab:**
- Host: wherever your Postgres lives (`localhost`, an IP, a hostname)
- Port: whatever you configured (`5432` by default)
- Database: whatever you named it during `hammer-pd-store init`
- Username / Password: your Postgres role and password

## Starter queries

Open the SQL Editor (Ctrl/Cmd-Enter, or right-click the connection → SQL
Editor → New SQL Editor). Useful starters:

```sql
-- What's in the cache right now?
SELECT stage, design, owner, triggering_user, dag_id,
       pg_size_pretty(size_bytes::bigint) AS size,
       created_at
FROM hammer_poc.pd_blobs
ORDER BY created_at DESC;

-- Who has registered workspaces?
SELECT username, workspace_root, updated_at
FROM hammer_poc.user_workspaces;

-- Cache size by design
SELECT design, count(*) AS rows,
       pg_size_pretty(sum(size_bytes)::bigint) AS total_size
FROM hammer_poc.pd_blobs
GROUP BY design
ORDER BY sum(size_bytes) DESC;

-- All par-input artifacts with their origin DAG
SELECT sha256, top_module, design, dag_id, triggering_user, created_at
FROM hammer_poc.pd_artifacts
ORDER BY created_at DESC;
```

To filter by a value in the grid view (no SQL needed): right-click a cell →
**Filter** → "Filter by value." DBeaver builds the `WHERE` clause for you.

## Tips

- **Save the connection.** DBeaver stores it in your workspace; it's there
  next time you open the app.
- **Auto-commit is on by default.** If you're poking around in a table and
  don't want to risk an accidental edit, switch the connection to read-only:
  right-click the connection → Edit Connection → General → check "Read-only
  connection."
- **Multiple connections at once.** DBeaver handles many connections in
  parallel tabs. Useful if you want to compare a dev cache to the team
  cache side-by-side.
- **Export.** Right-click a result set → Export Data → pick CSV, JSON,
  Excel, SQL inserts, etc.

## CLI alternative

If you don't want to install anything, every visualization above also works
via the CLI on bwrcix-3 (or wherever your CLI is set up):

```bash
hammer-pd-store list           # all blobs with provenance
hammer-pd-store blob-list      # short blob listing
hammer-pd-store master-list    # master databases
```

DBeaver is for ad-hoc exploration; the CLI is for "I'm already in a
terminal, just tell me what's there."
