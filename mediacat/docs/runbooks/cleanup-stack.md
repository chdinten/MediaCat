# Stack Cleanup --- Levels, Consequences, and Recovery

This runbook covers the three destructive cleanup options in
`mediacat-ctl.sh` (options 35--37). Each option operates at a different
blast radius: catalogue data only, full Docker volumes, or everything
Docker-managed system-wide. Read the relevant section before proceeding.

------------------------------------------------------------------------

## Quick-reference table

  ---------------------------------------------------------------------------
  Option   Label     What is destroyed    User data lost?    Recovery path
  -------- --------- -------------------- ------------------ ----------------
  **35**   `[!]`     Catalogue rows only  Catalogue content  Re-import data

  **36**   `[!!]`    All named Docker     DB + objects + TLS Migrations +
                     volumes              cert               cert re-import

  **37**   `[!!!]`   All unused Docker    Potentially all    Full rebuild of
                     data system-wide     projects           every project
  ---------------------------------------------------------------------------

------------------------------------------------------------------------

## Option 35 --- Catalogue data wipe `[!]`

### What it does

Truncates the following tables inside the running PostgreSQL container:

  ------------------------------------------------------------
  Table                      Contents
  -------------------------- ---------------------------------
  `tokens`                   Every vinyl / CD record in the
                             catalogue

  `token_revisions`          Edit history for each token

  `media_objects`            Image and file attachments

  `token_price_history`      Price tracking rows

  `token_symbols`            Barcode / catalogue number
                             symbols

  `ocr_artifacts`            OCR scan results

  `review_items`             Flagged or pending-review items

  `collection_import_jobs`   Discogs / MusicBrainz import job
                             records
  ------------------------------------------------------------

All truncations are issued with `CASCADE`, which removes dependent rows
automatically.

### What is preserved

- **User accounts** --- login credentials, roles, and sessions are
  untouched.
- **Database schema** --- tables, indexes, sequences, and constraints
  remain.
- **Reference data** --- labels, manufacturers, and countries are not
  touched.
- **MinIO objects** --- binary files stored in the object store are not
  deleted.
- **Caddy TLS certificate** --- HTTPS continues to work without
  re-importing.
- **Redis state** --- session cache and queue state are unaffected.

### When to use it

- Resetting test data without tearing down the stack.
- Clearing a staging environment before a new import run.
- Removing malformed bulk-import results without destroying
  infrastructure.

### Recovery

The stack remains fully operational after the wipe --- no restart
needed. Re-import catalogue content via the Discogs / MusicBrainz
ingestion routes in the web UI, or replay an earlier import job.

------------------------------------------------------------------------

## Option 36 --- Down + remove volumes `[!!]`

### What it does

Runs `docker compose down --volumes --remove-orphans`, which stops every
container and then deletes every **named Docker volume** associated with
the Compose project.

  ---------------------------------------------------
  Volume            Contents
  ----------------- ---------------------------------
  `postgres_data`   The entire PostgreSQL database
                    cluster

  `minio_data`      All MinIO objects (images, files)

  `caddy_data`      Caddy's TLS certificates and ACME
                    state

  `caddy_config`    Caddy runtime configuration cache
  ---------------------------------------------------

Bind-mounted directories on the host (source code, `.env`, seed files)
are **not** affected.

### When to use it

- Completely re-initialising a broken or corrupted database.
- Testing the full bootstrap procedure from zero.
- Removing all data before decommissioning an environment.

### Recovery procedure

Work through the following steps in order after a volume wipe.

#### 1 --- Start the stack

    # Option 1 in the menu, or directly:
    docker compose --env-file .env -f deploy/docker-compose.yaml up -d --remove-orphans

Wait for all services to report `healthy` (check with option 23 /
`docker compose ps`).

#### 2 --- Run database migrations

Use **option 21** in the menu, or run directly:

    docker compose --env-file .env -f deploy/docker-compose.yaml exec \
        -e PGHOST=postgres \
        -e PGUSER=mediacat_migrator \
        -e PGDATABASE=mediacat \
        app python -m alembic -c alembic.ini upgrade head

Alembic will apply every migration from the beginning and report the
final revision. Verify with **option 22** (`alembic current`).

#### 3 --- Re-import the Caddy root certificate into Windows

Caddy generates a new local CA on first start whenever its data volume
is empty. The previous certificate that was trusted by Windows is now
invalid; the new one must be imported, otherwise every browser will show
a TLS warning for `*.localhost` addresses.

**Step 3a --- Export the new certificate from the container (run in
WSL2):**

    docker compose --env-file .env -f deploy/docker-compose.yaml exec caddy \
        cat /data/caddy/pki/authorities/local/root.crt \
        > /tmp/caddy-root.crt

    # Copy to Windows Desktop so PowerShell can reach it
    cp /tmp/caddy-root.crt /mnt/c/Users/chdin/Desktop/

**Step 3b --- Import the certificate into the Windows trust store (run
in PowerShell as Administrator):**

    Import-Certificate `
        -FilePath C:\Users\chdin\Desktop\caddy-root.crt `
        -CertStoreLocation Cert:\CurrentUser\Root

**Step 3c --- Fully restart the browser.**

Opening a new tab is not sufficient --- the browser must be closed
completely and reopened so it re-reads the system certificate store.

#### 4 --- Re-import catalogue content

With the database empty and the stack running, use the web UI or the
ingestion CLI to re-import your collection from Discogs, MusicBrainz, or
another source.

------------------------------------------------------------------------

## Option 37 --- Full Docker system prune `[!!!]`` – ``NUCLEAR OPTION`

### What it does

Runs `docker system prune -af --volumes`, which removes from the Docker
daemon:

- All **stopped containers** (any project, not just MediaCat)
- All **unused images** (dangling and unreferenced)
- All **unused volumes** (including those from unrelated projects)
- All **unused networks**
- The entire **build cache**

> **This operation is system-wide.** It will destroy data from every
> Docker project on the machine, not only MediaCat. **Only use it when
> you are certain no other project has running containers or volumes you
> care about**.

### When to use it

- Reclaiming significant disk space on a development machine.
- Cleaning up after a long period of experimentation with many ephemeral
  containers.

### What will NOT be removed

- Containers that are currently **running** at the time the command
  executes.
- Volumes that are currently **attached to a running container**.

If the MediaCat stack is running when you trigger option 37, its volumes
are safe. Everything stopped or detached is gone.

### Recovery

There is no targeted recovery path --- by design this clears all unused
data.

1.  Start the MediaCat stack (option 1).
2.  Run migrations (option 21).
3.  Re-import the Caddy certificate (see Option 36, Steps 3a--3c above).
4.  Re-import catalogue content.
5.  Repeat the same steps for any other Docker project affected.

------------------------------------------------------------------------

## General notes

- Options 35, 36, and 37 all display a `WARNING` prompt and require an
  explicit `y` to proceed. Any other input cancels immediately and
  returns to the menu.
- After a volume wipe (36 or 37), always run migrations **before** using
  the web UI --- the app will fail on startup if the schema does not
  exist.
- The Caddy certificate must be re-imported whenever the `caddy_data`
  volume is destroyed, even if everything else is healthy. Missing this
  step results in browser TLS errors only; it does not affect the
  stack's internal HTTP communication.
