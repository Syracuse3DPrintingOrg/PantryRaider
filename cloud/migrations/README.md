# Forager schema migrations (Alembic)

The Forager cloud service manages its database schema with Alembic. Every
schema change is a versioned migration in `versions/`, applied forward with
`alembic upgrade head`. This replaced the old startup `create_all`-only
approach, which could add new tables and columns but could never alter or
retire an existing one.

## Layout

- `../alembic.ini` - Alembic config. It sets `script_location = migrations`
  and deliberately does NOT hard-code a database URL.
- `env.py` - reads the database URL from the app settings
  (`CLOUD_DATABASE_URL`) and builds the engine with the app's own
  `_make_engine`, so the SQLite StaticPool used by tests and the Postgres
  engine used in production behave exactly as they do at runtime. The
  autogenerate target is `Base.metadata` with every model imported, and
  `render_as_batch` is on for SQLite.
- `versions/` - the migration scripts. The first one (`down_revision = None`)
  is the baseline that captures the full schema as it existed before Alembic.
- `script.py.mako` - the template new revisions are rendered from.

All Alembic commands below are run from the `cloud/` directory (the same
directory that holds `alembic.ini`), with `CLOUD_DATABASE_URL` pointing at the
target database.

## One-time: adopt Alembic on the existing production database

The live VPS database already has every table (it was created by the old
`create_all`). Bring it under Alembic control WITHOUT changing any data by
stamping the baseline. This only writes the `alembic_version` bookkeeping row;
it creates, alters, and drops nothing.

```bash
# On the VPS, with the production CLOUD_DATABASE_URL in the environment:
cd cloud
alembic current      # expect: no version (empty) before stamping
alembic stamp head   # records the baseline revision; no schema/data change
alembic current      # expect: the baseline revision id, (head)
```

Run this once. After it, startup's `init_db` sees `alembic_version` and takes
the normal `alembic upgrade head` path on every boot.

The startup code never does this stamp for you: an existing database with app
tables but no `alembic_version` gets only the additive `create_all` that always
ran, exactly so a human decides when production adopts Alembic.

## Everyday workflow: making a schema change

1. Edit the models in `app/models.py`.
2. Autogenerate a migration and review it (autogenerate is a draft, not
   gospel: check the up/down operations, especially anything destructive):

   ```bash
   cd cloud
   alembic revision --autogenerate -m "describe the change"
   ```

3. Read and, if needed, hand-edit the new file in `versions/`.
4. Apply it locally and run the tests.

   ```bash
   alembic upgrade head
   python3 -m pytest -q
   ```

5. Commit the migration alongside the model change. On deploy, startup runs
   `alembic upgrade head` automatically (the database is already stamped), so
   the new migration applies on the next boot. You can also run
   `alembic upgrade head` by hand before rolling the service.

## Checking for drift

`alembic check` compares the models against the migrations and reports
`No new upgrade operations detected.` when they agree. Run it after editing
models to confirm a migration is still needed or has captured everything:

```bash
cd cloud
alembic check
```

## A note on fresh databases

A brand-new empty database (a fresh dev box, a new environment) is handled by
startup: `init_db` runs `create_all` and then stamps the baseline, so it comes
up already at head and ready for future migrations. The pytest suite keeps
using `create_all`/`drop_all` directly for speed and isolation.
