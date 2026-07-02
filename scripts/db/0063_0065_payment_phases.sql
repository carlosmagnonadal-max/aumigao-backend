-- Migrations 0063 + 0064 + 0065 (arquitetura de pagamento — Fases 1/2/3)
-- Gerado offline do alembic (dialeto PostgreSQL). Aplicar no Neon COMO DONO.
-- Atualiza alembic_version ao final de cada etapa (mantém o alembic em sincronia).
-- Pré-condição: alembic_version atual = '0062_tax_regime'.

BEGIN;

-- ============ 0063: comissão medida do tenant ============
CREATE TABLE commission_entries (
    id VARCHAR NOT NULL,
    tenant_id VARCHAR NOT NULL,
    walk_id VARCHAR NOT NULL,
    period VARCHAR NOT NULL,
    walk_price FLOAT NOT NULL,
    commission_percent FLOAT NOT NULL,
    amount FLOAT NOT NULL,
    is_network BOOLEAN DEFAULT false NOT NULL,
    status VARCHAR DEFAULT 'accrued' NOT NULL,
    asaas_payment_id VARCHAR,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    billed_at TIMESTAMP WITH TIME ZONE,
    paid_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id)
);
ALTER TABLE commission_entries ADD CONSTRAINT uq_commission_entries_walk_id UNIQUE (walk_id);
CREATE INDEX ix_commission_entries_tenant_id ON commission_entries (tenant_id);
CREATE INDEX ix_commission_entries_period ON commission_entries (period);
CREATE INDEX ix_commission_entries_status ON commission_entries (status);
CREATE INDEX ix_commission_entries_tenant_period_status ON commission_entries (tenant_id, period, status);
UPDATE alembic_version SET version_num='0063_commission_entries' WHERE version_num='0062_tax_regime';

-- ============ 0064: ledger-fornecedor do passeador da rede ============
CREATE TABLE walker_earnings (
    id VARCHAR NOT NULL,
    walker_id VARCHAR NOT NULL,
    tenant_id VARCHAR,
    walk_id VARCHAR NOT NULL,
    gross FLOAT NOT NULL,
    platform_amount FLOAT NOT NULL,
    amount FLOAT NOT NULL,
    status VARCHAR DEFAULT 'accrued' NOT NULL,
    accrued_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    payable_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id)
);
ALTER TABLE walker_earnings ADD CONSTRAINT uq_walker_earnings_walk_id UNIQUE (walk_id);
CREATE INDEX ix_walker_earnings_walker_id ON walker_earnings (walker_id);
CREATE INDEX ix_walker_earnings_tenant_id ON walker_earnings (tenant_id);
CREATE INDEX ix_walker_earnings_status ON walker_earnings (status);
UPDATE alembic_version SET version_num='0064_walker_earnings' WHERE version_num='0063_commission_entries';

-- ============ 0065: colunas de estorno (void) ============
ALTER TABLE walker_earnings ADD COLUMN void_reason VARCHAR;
ALTER TABLE walker_earnings ADD COLUMN voided_at TIMESTAMP WITH TIME ZONE;
UPDATE alembic_version SET version_num='0065_walker_earning_void' WHERE version_num='0064_walker_earnings';

COMMIT;
