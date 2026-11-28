BEGIN;
-- Preserve pre-006 rows faithfully: unit stays NULL rather than backfilling
-- from the current active forecast_targets.unit.
ALTER TABLE predictions ADD COLUMN unit text CHECK (unit IS NULL OR unit <> '');
COMMIT;
