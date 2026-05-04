---
name: spainexpat-lightsail-backup
description: Back up the legacy SpainExpat ExpressionEngine production site on Lightsail, including the RDS database and Apache htdocs, leaving artifacts on the server.
version: 1.0.0
author: Spanorama
license: MIT
---

# SpainExpat Lightsail backup

Use this when Tyler wants a pre-upgrade or pre-maintenance backup of the live legacy SpainExpat site.

## Context
- Host: `15.223.89.243`
- SSH user: `bitnami`
- SSH key: `~/.ssh/LightsailDefaultKey-ca-central-1.pem`
- Web root: `/opt/bitnami/apache/htdocs`
- EE config: `/opt/bitnami/apache/htdocs/ee_system/user/config/config.php`
- Backups live on-server under `/home/bitnami/backups/`

## Important finding
The RDS dump path can fail with a TLS certificate validation error if you use plain `mysqldump` defaults. The working approach was:
- use `/opt/bitnami/mariadb/bin/mariadb-dump`
- add `--ssl=0`

Without that, the dump failed with:
`TLS/SSL error: unable to get local issuer certificate`

## Steps

### 1. Verify SSH access and disk space
```bash
ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -i ~/.ssh/LightsailDefaultKey-ca-central-1.pem bitnami@15.223.89.243 'hostname; whoami; pwd; df -h / /opt/bitnami/apache/htdocs /home/bitnami; du -sh /opt/bitnami/apache/htdocs 2>/dev/null'
```

### 2. Read live DB credentials from EE config
Do not guess DB credentials. Read them from the live server config:
```bash
ssh -o BatchMode=yes -i ~/.ssh/LightsailDefaultKey-ca-central-1.pem bitnami@15.223.89.243 "nl -ba /opt/bitnami/apache/htdocs/ee_system/user/config/config.php | sed -n '58,68p'"
```

Expected structure:
- nested under `$config['database']['expressionengine']`
- `hostname`
- `username`
- `password`
- `database`

Note: a naive search for `db_hostname` / `db_name` will fail on this install because EE stores the live DB settings in the nested `database` array instead.

### 3. Create the backup on the server
Run in background if needed because the htdocs tarball is large.

Template:
```bash
ssh -o BatchMode=yes -i ~/.ssh/LightsailDefaultKey-ca-central-1.pem bitnami@15.223.89.243 'set -euo pipefail
TS=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR=/home/bitnami/backups/spainexpat_$TS
mkdir -p "$BACKUP_DIR"
MYSQL_PWD='"'"'<DB_PASSWORD>'"'"' /opt/bitnami/mariadb/bin/mariadb-dump --ssl=0 --single-transaction --routines --triggers --events -h <DB_HOST> -u <DB_USER> <DB_NAME> | gzip -1 > "$BACKUP_DIR/<DB_NAME>_${TS}.sql.gz"
tar -C /opt/bitnami/apache -czf "$BACKUP_DIR/htdocs_${TS}.tar.gz" htdocs
printf "BACKUP_DIR=%s\n" "$BACKUP_DIR"
ls -lh "$BACKUP_DIR"'
```

### 4. Verify artifacts
```bash
ssh -o BatchMode=yes -i ~/.ssh/LightsailDefaultKey-ca-central-1.pem bitnami@15.223.89.243 'set -euo pipefail
BACKUP_DIR=/home/bitnami/backups/spainexpat_<TIMESTAMP>
gzip -t "$BACKUP_DIR"/*.sql.gz
tar -tzf "$BACKUP_DIR"/htdocs_*.tar.gz >/dev/null
sha256sum "$BACKUP_DIR"/*'
```

## What to report back
Report:
- backup directory path
- DB dump filename and size
- files tarball filename and size
- verification status
- SHA-256 for both files

## Known-good result from 2026-04-20
- Backup dir: `/home/bitnami/backups/spainexpat_20260420_161053`
- DB dump: `secom_spainexpat_20260420_161053.sql.gz` (`63M`)
- Files tarball: `htdocs_20260420_161053.tar.gz` (`913M`)
- SHA-256:
  - `043f93e1f0aeec0a939f769afd2fce23c8ab293452bbece2b81b770ae416c9f6`  `htdocs_20260420_161053.tar.gz`
  - `6bbba295c9e8b563775f8d631cf1a50f8b5548b47062d2cb629a42b651468829`  `secom_spainexpat_20260420_161053.sql.gz`

## Pitfalls
- `claude -p` may be unavailable if Claude CLI is not logged in on this machine.
- Local copies of project files may be stale, empty, or placeholders; prefer the live server config for authoritative DB credentials.
- `mysqldump` may emit a deprecation warning and then fail on TLS; use `mariadb-dump --ssl=0` instead.
- `tar -tzf ... | head` can exit 141 due to broken pipe; for verification, prefer `tar -tzf ... >/dev/null`.
