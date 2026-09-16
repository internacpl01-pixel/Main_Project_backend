-- Persistent record of every file /imports/from-drive (and its retry
-- endpoint) has ever touched -- outlives the in-memory job registry
-- (services/jobs.py), which is pruned 15 minutes after a job finishes and
-- lost entirely on a restart. This is what answers "what happened to that
-- statement last Tuesday and why did it fail" after the run itself is long
-- gone from screen -- confirmed with the user.
CREATE TABLE IF NOT EXISTS drive_import_log (
    id          bigserial   PRIMARY KEY,
    file_name   text        NOT NULL,
    status      text        NOT NULL
                CHECK (status IN ('done', 'failed', 'password_required', 'skipped')),
    error       text,
    row_count   integer,
    bank_id     bigint      REFERENCES bank_master(id) ON DELETE SET NULL,
    imported_by text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_drive_import_log_created_at
    ON drive_import_log (created_at DESC);
