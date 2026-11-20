#!/usr/bin/env bash
set -euo pipefail
umask 077

infra=$(cd -- "$(dirname -- "$0")/.." && pwd)
: "${OPS_ENV:?absolute path to Compose env file}"
: "${OPS_PROJECT:?Compose project name}"
compose=(docker compose -p "$OPS_PROJECT" --env-file "$OPS_ENV" -f "$infra/compose.prod.yaml")
db=("${compose[@]}" exec -T --user postgres db)
psql=("${db[@]}" psql -X -v ON_ERROR_STOP=1 -U telemetry_admin -d telemetry)

case "${1:-}" in
    migrate)
        # Fresh database only. Explicit execution keeps restart separate from schema changes.
        [[ -f "$infra/../backend/aggregation.py" && -f "$infra/../backend/forecast.py" ]] || {
            echo 'Deployment revision must include aggregation and forecast workers' >&2
            exit 1
        }
        [[ "$("${psql[@]}" -Atc "SELECT count(*) FROM pg_tables WHERE schemaname='public'")" == 0 ]] || {
            echo 'Initial migration requires an empty database' >&2
            exit 1
        }
        for migration in "$infra"/../database/migrations/*.sql; do
            "${psql[@]}" < "$migration"
        done
        "${psql[@]}" < "$infra/prod/grants.sql"
        ;;
    maintain)
        "${psql[@]}" < "$infra/prod/maintenance.sql"
        echo partition_maintenance_success
        ;;
    check)
        : "${OPS_TLS_HOST:?public TLS hostname}"
        : "${OPS_DISK_PATH:?filesystem containing Docker data}"
        status=0
        for service in db api aggregation forecast proxy; do
            container=$("${compose[@]}" ps --status running -q "$service")
            if [[ -z "$container" ]]; then
                echo "$service is down" >&2
                status=1
            fi
        done
        if ! "${compose[@]}" exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/partitions', timeout=6)"; then
            echo 'API or partition horizon check failed' >&2
            status=1
        fi
        "${psql[@]}" -q < "$infra/prod/check.sql" || status=1
        chronyc waitsync 1 0.1 || status=1
        curl --fail --silent --show-error --max-time 10 "https://$OPS_TLS_HOST/openapi.json" >/dev/null || status=1
        if ! timeout 15 openssl s_client -connect "$OPS_TLS_HOST:443" -servername "$OPS_TLS_HOST" </dev/null 2>/dev/null |
            openssl x509 -noout -enddate -checkend 1209600; then
            echo 'TLS expiry check failed (14-day threshold)' >&2
            status=1
        fi
        for df_options in -P -Pi; do
            if ! df "$df_options" "$OPS_DISK_PATH" | awk 'NR == 2 { ok = ($5 + 0 < 85) } END { exit !ok }'; then
                echo "Disk usage check failed ($df_options, 85% threshold)" >&2
                status=1
            fi
        done
        exit "$status"
        ;;
    backup|restore)
        : "${OPS_MODELS:?absolute host path to the model directory}"
        : "${OPS_SCRATCH:?private scratch directory on encrypted storage}"
        archive=${2:?encrypted archive path}
        scratch=$(mktemp -d "$OPS_SCRATCH/s402.XXXXXXXX")
        trap 'rm -rf -- "$scratch"' EXIT
        if [[ "$1" == backup ]]; then
            : "${OPS_AGE_RECIPIENT:?age recipient}"
            [[ ! -e "$archive" ]]
            "${db[@]}" pg_dump -U telemetry_admin -d telemetry --format=custom > "$scratch/db.dump"
            tar -C "$OPS_MODELS" -cf "$scratch/models.tar" .
            tar -C "$scratch" -cf - db.dump models.tar |
                age -r "$OPS_AGE_RECIPIENT" -o "$scratch/backup.age"
            # Link atomically without replacing an existing backup.
            partial=$(mktemp "${archive}.XXXXXXXX")
            trap 'rm -rf -- "$scratch"; rm -f -- "$partial"' EXIT
            cp -- "$scratch/backup.age" "$partial"
            ln -- "$partial" "$archive"
            sha256sum -- "$archive"
        else
            [[ "$OPS_PROJECT" == telemetry-restore-* ]] || {
                echo 'Use a dedicated telemetry-restore-* Compose project' >&2
                exit 1
            }
            : "${OPS_AGE_IDENTITY:?age identity path}"
            : "${OPS_ARCHIVE_SHA256:?digest from the trusted backup inventory}"
            actual=$(sha256sum < "$archive")
            [[ "${actual%% *}" == "$OPS_ARCHIVE_SHA256" ]]
            [[ -d "$OPS_MODELS" ]]
            entries=$(find "$OPS_MODELS" -mindepth 1 -maxdepth 1 -print -quit)
            [[ -z "$entries" ]]
            [[ "$("${compose[@]}" ps --status running --services)" == db ]]
            [[ "$("${psql[@]}" -Atc "SELECT count(*) FROM pg_tables WHERE schemaname='public'")" == 0 ]]
            age -d -i "$OPS_AGE_IDENTITY" -o "$scratch/bundle.tar" "$archive"
            tar -C "$scratch" -xf "$scratch/bundle.tar" db.dump models.tar
            tar -tf "$scratch/models.tar" >/dev/null
            "${db[@]}" pg_restore -U telemetry_admin -d telemetry --exit-on-error --single-transaction < "$scratch/db.dump"
            tar --no-same-owner -C "$OPS_MODELS" -xf "$scratch/models.tar"
            ;;
        esac
        ;;
    *)
        echo 'Usage: ops.sh migrate|maintain|check|backup ARCHIVE|restore ARCHIVE' >&2
        exit 2
        ;;
esac
