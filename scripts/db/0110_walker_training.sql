-- 0110_walker_training — Capacitação do passeador (S2).
-- Aplicar no Neon SQL Editor (role owner) DEPOIS da 0109_walk_emergency_calls.
-- Pré-condição: SELECT version_num FROM alembic_version;  -> '0109_walk_emergency_calls'
BEGIN;

CREATE TABLE IF NOT EXISTS walker_training_progress (
    id VARCHAR PRIMARY KEY,
    walker_user_id VARCHAR NOT NULL,
    content_version VARCHAR NOT NULL,
    module_id VARCHAR NOT NULL,
    best_score INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    passed_at TIMESTAMP WITHOUT TIME ZONE NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE NULL,
    CONSTRAINT uq_walker_training_progress_module UNIQUE (walker_user_id, content_version, module_id)
);
CREATE INDEX IF NOT EXISTS ix_walker_training_progress_walker_user_id
    ON walker_training_progress (walker_user_id);

ALTER TABLE walker_profiles ADD COLUMN IF NOT EXISTS training_completed_version VARCHAR NULL;
ALTER TABLE walker_profiles ADD COLUMN IF NOT EXISTS training_completed_at TIMESTAMP WITHOUT TIME ZONE NULL;

-- Tabela global (sem RLS); o role da app precisa de DML (padrão da 0050).
GRANT SELECT, INSERT, UPDATE, DELETE ON walker_training_progress TO aumigao_app;

UPDATE alembic_version SET version_num = '0110_walker_training'
 WHERE version_num = '0109_walk_emergency_calls';

COMMIT;
