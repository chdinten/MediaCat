# Runbooks

Operational procedures. Each runbook is a file in this directory:

| File                                   | Purpose                              |
|----------------------------------------|--------------------------------------|
| [`control-panel.md`](control-panel.md) | Addresses (`:8443`) and the web control panel |
| [`backup-restore.md`](backup-restore.md) | Daily pg_dump, manual backup, tested restore |
| `rotate-secrets.md` *(Section 2)*      | DB, MinIO, and app secret rotation   |
| `migrate-database.md` *(Section 3)*    | Alembic upgrade / downgrade          |
| `handle-ingestion-drift.md` *(Sec. 5)* | What to do when schema drift fires   |
| [`cleanup-stack.md`](cleanup-stack.md) | Cleanup levels [!]/[!!]/[!!!]: consequences and recovery |

Files land alongside the sections that introduce the capability.
