# Sourced by the PostgreSQL entrypoint during first initialization.
(
    set -eu
    # The single pgpass entry uses a hex password so no escaping is needed.
    TELEMETRY_ROLE_PASSWORD=$(cut -d: -f5 /run/secrets/bootstrap_pgpass)
    case "$TELEMETRY_ROLE_PASSWORD" in
        ''|*[!0-9a-f]*) exit 1 ;;
    esac
    [ "${#TELEMETRY_ROLE_PASSWORD}" -eq 64 ]
    export TELEMETRY_ROLE_PASSWORD
    docker_process_sql -q -v ON_ERROR_STOP=1 >/dev/null 2>&1 <<'SQL' || {
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
\getenv role_password TELEMETRY_ROLE_PASSWORD
CREATE ROLE telemetry_app LOGIN PASSWORD :'role_password';
REVOKE CONNECT ON DATABASE telemetry FROM PUBLIC;
GRANT CONNECT ON DATABASE telemetry TO telemetry_app;
SQL
        echo 'Database role initialization failed' >&2
        exit 1
    }
)
