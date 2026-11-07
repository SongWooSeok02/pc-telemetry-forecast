BEGIN;
-- Sessions are provisioned only by the trusted login adapter, never by device APIs.
CREATE TABLE user_sessions (
 token_sha256 text PRIMARY KEY CHECK(token_sha256 ~ '^[0-9a-f]{64}$'),
 user_id uuid NOT NULL, expires_at timestamptz NOT NULL, revoked boolean NOT NULL DEFAULT false
);
CREATE TABLE machines (
 machine_id uuid PRIMARY KEY, owner_id uuid NOT NULL, deleted boolean NOT NULL DEFAULT false
);
CREATE TABLE registration_codes (
 code_sha256 text PRIMARY KEY, owner_id uuid NOT NULL, machine_id uuid,
 expires_at timestamptz NOT NULL, consumed boolean NOT NULL DEFAULT false
);
CREATE INDEX registration_owner_idx ON registration_codes(owner_id);
CREATE TABLE credential_audit (
 event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 occurred_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 actor_id uuid NOT NULL, machine_id uuid, event text NOT NULL
 CHECK(event IN ('code_issued','registered','reissued','revoked','deleted'))
);
COMMIT;
