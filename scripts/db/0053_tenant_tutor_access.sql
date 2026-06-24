-- ── tenant_tutor_access (Modelo B white-label) ───────────────────────────────
-- Rodar como DONO (neondb_owner) no Neon SQL Editor ANTES do deploy do backend.
CREATE TABLE IF NOT EXISTS tenant_tutor_access (
    id            VARCHAR PRIMARY KEY,
    tenant_id     VARCHAR NOT NULL REFERENCES tenants(id),
    tutor_user_id VARCHAR NOT NULL REFERENCES users(id),
    status        VARCHAR NOT NULL DEFAULT 'active',
    initiated_by  VARCHAR(16) NOT NULL DEFAULT 'tutor',
    created_at    TIMESTAMP,
    updated_at    TIMESTAMP,
    CONSTRAINT uq_tenant_tutor_access_tenant_tutor UNIQUE (tenant_id, tutor_user_id)
);
CREATE INDEX IF NOT EXISTS ix_tenant_tutor_access_tenant_id     ON tenant_tutor_access(tenant_id);
CREATE INDEX IF NOT EXISTS ix_tenant_tutor_access_tutor_user_id ON tenant_tutor_access(tutor_user_id);
CREATE INDEX IF NOT EXISTS ix_tenant_tutor_access_status        ON tenant_tutor_access(status);

ALTER TABLE tenant_tutor_access ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_tutor_access_isolation ON tenant_tutor_access;
CREATE POLICY tenant_tutor_access_isolation ON tenant_tutor_access
  USING ( current_setting('app.current_tenant', true) = '*'
          OR tenant_id = current_setting('app.current_tenant', true) )
  WITH CHECK ( current_setting('app.current_tenant', true) = '*'
               OR tenant_id = current_setting('app.current_tenant', true) );
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_tutor_access TO aumigao_app;
