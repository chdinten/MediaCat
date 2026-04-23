# Runbooks

Operational procedures. Each runbook is a file in this directory:

| File                                   | Purpose                              |
|----------------------------------------|--------------------------------------|
| `backup-restore.md` *(Section 2)*      | pg_dump + MinIO mirror + restore     |
| `rotate-secrets.md` *(Section 2)*      | DB, MinIO, and app secret rotation   |
| `migrate-database.md` *(Section 3)*    | Alembic upgrade / downgrade          |
| `handle-ingestion-drift.md` *(Sec. 5)* | What to do when schema drift fires   |

Files land alongside the sections that introduce the capability.
