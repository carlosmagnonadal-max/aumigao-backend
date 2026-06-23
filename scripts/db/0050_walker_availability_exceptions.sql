-- 0050_walker_availability_exceptions.sql
-- Equivalente SQL da migration Alembic 0050. Rodar no Neon SQL Editor (dono). Idempotente.
CREATE TABLE IF NOT EXISTS walker_availability_exceptions (
    id              VARCHAR PRIMARY KEY,
    walker_user_id  VARCHAR NOT NULL REFERENCES users(id),
    exception_date  DATE NOT NULL,
    kind            VARCHAR(8) NOT NULL,
    start_time      VARCHAR(5),
    end_time        VARCHAR(5),
    created_at      TIMESTAMP NOT NULL DEFAULT now(),
    updated_at      TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_wae_walker_user_id ON walker_availability_exceptions (walker_user_id);
CREATE INDEX IF NOT EXISTS ix_wae_exception_date ON walker_availability_exceptions (exception_date);
ALTER TABLE walker_availability_exceptions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS walker_self_isolation ON walker_availability_exceptions;
CREATE POLICY walker_self_isolation ON walker_availability_exceptions
  USING ( current_setting('app.current_tenant', true) = '*' OR walker_user_id = current_setting('app.current_user_id', true) )
  WITH CHECK ( walker_user_id = current_setting('app.current_user_id', true) );
GRANT SELECT, INSERT, UPDATE, DELETE ON walker_availability_exceptions TO aumigao_app;
