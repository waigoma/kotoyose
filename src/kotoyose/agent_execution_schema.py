"""Durable logical execution tables, independent of the existing attempt schema."""

_SCHEMA = """
DO $$ BEGIN
 IF to_regclass('kotoyose.agent_executions') IS NULL AND to_regclass('kotoyose.hub_executions') IS NOT NULL THEN
  DROP TRIGGER IF EXISTS hub_execution_receipts_immutable ON kotoyose.hub_execution_receipts;
  DROP TRIGGER IF EXISTS hub_execution_authorizations_immutable ON kotoyose.hub_execution_authorizations;
  DROP TRIGGER IF EXISTS hub_execution_cancellations_immutable ON kotoyose.hub_execution_cancellations;
  DROP FUNCTION IF EXISTS kotoyose.reject_hub_immutable_mutation();
  ALTER TABLE IF EXISTS kotoyose.hub_executions RENAME TO agent_executions;
  ALTER INDEX IF EXISTS kotoyose.hub_predecessor_once RENAME TO agent_execution_predecessor_once;
  ALTER TABLE IF EXISTS kotoyose.hub_execution_snapshot_digests RENAME TO agent_execution_snapshot_digests;
  ALTER TABLE IF EXISTS kotoyose.hub_execution_activations RENAME TO agent_execution_activations;
  ALTER TABLE IF EXISTS kotoyose.hub_execution_charge_units RENAME TO agent_execution_charge_units;
  ALTER TABLE IF EXISTS kotoyose.hub_execution_receipts RENAME TO agent_execution_receipts;
  ALTER TABLE IF EXISTS kotoyose.hub_execution_authorizations RENAME TO agent_execution_authorizations;
  ALTER TABLE IF EXISTS kotoyose.hub_execution_actions RENAME TO agent_execution_actions;
  ALTER TABLE IF EXISTS kotoyose.hub_execution_cancellations RENAME TO agent_execution_cancellations;
  ALTER TABLE IF EXISTS kotoyose.hub_execution_input_pins RENAME TO agent_execution_input_pins;
  ALTER TABLE IF EXISTS kotoyose.hub_execution_client_replays RENAME TO agent_execution_client_replays;
 END IF;
END $$;
CREATE TABLE IF NOT EXISTS kotoyose.agent_executions (
 id uuid PRIMARY KEY, principal text NOT NULL, idempotency_key text NOT NULL,
 request_digest text NOT NULL, work_digest text NOT NULL, caller_run_ref text NOT NULL,
 request jsonb NOT NULL, pinned_plan jsonb NOT NULL,
 predecessor_execution_id text, predecessor_key text, predecessor_relation text,
 state text NOT NULL, state_revision bigint NOT NULL, sequence bigint NOT NULL,
 current_nonce text NOT NULL, authorization_revision bigint NOT NULL,
 settled_receipt_cursor bigint NOT NULL, fault_retries_used integer NOT NULL CHECK (fault_retries_used IN (0,1)),
 cancel_requested boolean NOT NULL, admission_closed boolean NOT NULL,
 authority_required boolean NOT NULL, pause jsonb, provider_limit jsonb, next_probe_at timestamptz,
 probe_step integer NOT NULL DEFAULT 0, last_try_now_at timestamptz, terminal jsonb,
 workspace_id text NOT NULL, workspace_generation bigint NOT NULL, workspace_ref text,
 created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
 UNIQUE (principal,idempotency_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_execution_predecessor_once ON kotoyose.agent_executions(principal,predecessor_execution_id)
 WHERE predecessor_execution_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS kotoyose.agent_execution_snapshot_digests (
 execution_id uuid PRIMARY KEY REFERENCES kotoyose.agent_executions(id),
 digest text NOT NULL, sequence bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS kotoyose.agent_execution_activations (
 execution_id uuid NOT NULL REFERENCES kotoyose.agent_executions(id), nonce text NOT NULL, ordinal bigint NOT NULL,
 reason text NOT NULL CHECK (reason IN ('initial','provider_resume','fault_retry','approval_continue','lease_resume')),
 unit_ordinal bigint NOT NULL, state text NOT NULL CHECK (state IN ('awaiting','starting','running','ended','closed')),
 attempt_id text UNIQUE, launch text NOT NULL CHECK (launch IN ('not_started','started','unknown')),
 measurement text NOT NULL CHECK (measurement IN ('none','complete','unknown')),
 active_ms bigint NOT NULL DEFAULT 0, turns bigint, output_bytes bigint, provider_rejected_only boolean NOT NULL DEFAULT false,
 approval jsonb, interruption jsonb, started_at timestamptz, finished_at timestamptz,
 PRIMARY KEY (execution_id,nonce), UNIQUE (execution_id,ordinal)
);
CREATE TABLE IF NOT EXISTS kotoyose.agent_execution_charge_units (
 execution_id uuid NOT NULL REFERENCES kotoyose.agent_executions(id), unit_ordinal bigint NOT NULL,
 charge_ref text, reservation_ref text, allocation_units text,
 state text NOT NULL CHECK (state IN ('open','closed','unprovable')), receipt_cursor bigint,
 PRIMARY KEY (execution_id,unit_ordinal), UNIQUE (execution_id,charge_ref), UNIQUE (execution_id,reservation_ref)
);
CREATE TABLE IF NOT EXISTS kotoyose.agent_execution_receipts (
 execution_id uuid NOT NULL REFERENCES kotoyose.agent_executions(id), cursor bigint NOT NULL,
 receipt_id text NOT NULL UNIQUE, unit_ordinal bigint NOT NULL, body jsonb NOT NULL, digest text NOT NULL,
 PRIMARY KEY(execution_id,cursor), UNIQUE (execution_id,unit_ordinal)
);
CREATE TABLE IF NOT EXISTS kotoyose.agent_execution_authorizations (
 execution_id uuid NOT NULL REFERENCES kotoyose.agent_executions(id), revision bigint NOT NULL,
 nonce text NOT NULL, decision text NOT NULL, body jsonb NOT NULL, digest text NOT NULL,
 PRIMARY KEY(execution_id,revision)
);
CREATE TABLE IF NOT EXISTS kotoyose.agent_execution_actions (
 execution_id uuid NOT NULL REFERENCES kotoyose.agent_executions(id), action_key text NOT NULL,
 kind text NOT NULL CHECK (kind IN ('try_now','cancel')), body_digest text NOT NULL, created_at timestamptz NOT NULL,
 PRIMARY KEY(execution_id,action_key)
);
CREATE TABLE IF NOT EXISTS kotoyose.agent_execution_cancellations (
 principal text NOT NULL, idempotency_key text NOT NULL, action_key text NOT NULL, reason text NOT NULL,
 proof_ref text NOT NULL UNIQUE, created_at timestamptz NOT NULL, PRIMARY KEY(principal,idempotency_key)
);
CREATE TABLE IF NOT EXISTS kotoyose.agent_execution_input_pins (
 execution_id uuid NOT NULL REFERENCES kotoyose.agent_executions(id), artifact_ref text NOT NULL,
 principal text NOT NULL, released_at timestamptz, PRIMARY KEY(execution_id,artifact_ref)
);
CREATE TABLE IF NOT EXISTS kotoyose.agent_execution_client_replays (
 execution_id uuid NOT NULL REFERENCES kotoyose.agent_executions(id),
 call_id text NOT NULL REFERENCES kotoyose.execution_client_calls(id),
 tool_ref text NOT NULL, arguments_digest text NOT NULL, consumed_by text,
 PRIMARY KEY (execution_id,call_id)
);
CREATE OR REPLACE FUNCTION kotoyose.reject_agent_execution_immutable_mutation() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'agent execution record is immutable'; END; $$ LANGUAGE plpgsql;
DO $$ DECLARE table_name text; trigger_name text; BEGIN
 FOREACH table_name IN ARRAY ARRAY['agent_execution_receipts','agent_execution_authorizations','agent_execution_cancellations'] LOOP
  trigger_name := table_name || '_immutable';
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname=trigger_name AND tgrelid=('kotoyose.' || table_name)::regclass) THEN
   EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON kotoyose.%I FOR EACH ROW EXECUTE FUNCTION kotoyose.reject_agent_execution_immutable_mutation()', trigger_name, table_name);
  END IF;
 END LOOP;
END $$;
"""


async def initialize_agent_execution_schema(pool) -> None:
    await pool.execute(_SCHEMA)
