-- 0111_local_rules_pet_reactive — Regras locais estruturadas + cão reativo (S3). Equivalente SQL da migration Alembic 0111.
-- Aplicar no Neon SQL Editor (role dono neondb_owner) DEPOIS da 0110_walker_training.sql e ANTES do deploy do backend.
-- Pré-condição: SELECT version_num FROM alembic_version;  -> '0110_walker_training'
-- Idempotente: IF NOT EXISTS / ON CONFLICT DO NOTHING / DROP POLICY IF EXISTS; o backfill só toca pets com is_reactive = false.
-- Seed gerado a partir de SEED_ROWS da migration (mesmos ids fixos e textos). Nenhuma regra `limite_caes` (D7/D8).
BEGIN;

-- Trava de ordem: só segue se o banco estiver na 0110 (primeira vez) ou já na 0111 (reexecução).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM alembic_version
         WHERE version_num IN ('0110_walker_training', '0111_local_rules_pet_reactive')
    ) THEN
        RAISE EXCEPTION 'alembic_version inesperada (esperado 0110_walker_training). Rode 0109 e 0110 antes. Nada foi aplicado.';
    END IF;
END $$;

-- Escopo global para o backfill em pets (RLS-ON). O dono já não é forçado (0043 sem FORCE); é só defesa.
SELECT set_config('app.current_tenant', '*', true);

-- (a) Tabela GLOBAL local_rules (sem tenant_id).
CREATE TABLE IF NOT EXISTS local_rules (
    id             VARCHAR PRIMARY KEY,
    uf             VARCHAR(2) NOT NULL,
    municipio      VARCHAR NULL,
    nivel          VARCHAR NOT NULL,
    tema           VARCHAR NOT NULL,
    criterio       VARCHAR NOT NULL,
    params_json    TEXT NOT NULL DEFAULT '{}',
    exigencia_json TEXT NOT NULL DEFAULT '{}',
    norma          VARCHAR NOT NULL,
    fonte_url      VARCHAR NULL,
    verificado_em  DATE NULL,
    confianca      VARCHAR NOT NULL DEFAULT 'media',
    status         VARCHAR NOT NULL DEFAULT 'vigente',
    confirmado_por VARCHAR NOT NULL DEFAULT 'plataforma',
    created_at     TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    updated_at     TIMESTAMP WITHOUT TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_local_rules_uf     ON local_rules (uf);
CREATE INDEX IF NOT EXISTS ix_local_rules_tema   ON local_rules (tema);
CREATE INDEX IF NOT EXISTS ix_local_rules_status ON local_rules (status);

-- (b) pets.is_reactive / pets.reactivity_notes.
ALTER TABLE pets ADD COLUMN IF NOT EXISTS is_reactive BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE pets ADD COLUMN IF NOT EXISTS reactivity_notes TEXT NULL;

-- Backfill: is_reactive = true onde behavior_notes contém "reativo", EXCETO quando a
-- única ocorrência é negada ("não reativo"/"nao reativo") — mesma regra de
-- _note_marks_reactive() da migration (regex sem diferenciar maiúsculas).
UPDATE pets
   SET is_reactive = true
 WHERE is_reactive = false
   AND behavior_notes ~* 'reativo'
   AND regexp_replace(behavior_notes, 'n[aãAÃ]o\s+reativo', ' ', 'gi') ~* 'reativo';

-- (c) Seed idempotente por id fixo (não sobrescreve linha existente, igual à migration).
INSERT INTO local_rules (id, uf, municipio, nivel, tema, criterio, params_json, exigencia_json, norma, fonte_url, verificado_em, confianca, status, confirmado_por, created_at, updated_at)
VALUES
    ('lr-ba-salvador-focinheira-peso',
     'BA',
     'Salvador',
     'municipal',
     'focinheira',
     'weight_min_kg',
     '{"inclusive": false, "min_kg": 24, "size_fallback": ["grande", "gigante"]}',
     '{"detalhe": "Cães de grande porte (acima de 24 kg) e de porte gigante, em ambiente público ou privado de uso coletivo, sempre acompanhados do responsável.", "itens": ["guia", "focinheira"], "onde": "em local público ou privado de uso coletivo"}',
     'Lei Municipal nº 9.108/2016 (Salvador), arts. 5º e 11',
     'https://leismunicipais.com.br/a1/ba/s/salvador/lei-ordinaria/2016/911/9108',
     DATE '2026-09-15',
     'alta',
     'vigente',
     'plataforma',
     (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')),
    ('lr-ba-salvador-focinheira-reativo',
     'BA',
     'Salvador',
     'municipal',
     'focinheira',
     'reactive',
     '{}',
     '{"itens": ["guia", "focinheira"], "nota": "A lei usa o termo ''bravios'' sem definição; aplicado a cães declarados reativos (padrão protetivo).", "onde": "em local público"}',
     'Lei Municipal nº 9.108/2016 (Salvador), art. 11',
     'https://leismunicipais.com.br/a1/ba/s/salvador/lei-ordinaria/2016/911/9108',
     DATE '2026-09-15',
     'media',
     'vigente',
     'plataforma',
     (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')),
    ('lr-ba-salvador-guia-afluxo',
     'BA',
     'Salvador',
     'municipal',
     'guia',
     'location',
     '{"locais": ["grande afluxo de pessoas"]}',
     '{"itens": ["guia"], "onde": "em ambiente de grande afluxo de pessoas"}',
     'Lei Municipal nº 9.108/2016 (Salvador), art. 10',
     'https://leismunicipais.com.br/a1/ba/s/salvador/lei-ordinaria/2016/911/9108',
     DATE '2026-09-15',
     'alta',
     'vigente',
     'plataforma',
     (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')),
    ('lr-ba-salvador-praia',
     'BA',
     'Salvador',
     'municipal',
     'praia',
     'location',
     '{"locais": ["praia"]}',
     '{"itens": ["guia"], "nota": "A Lei 9.108/2016 (art. 10) permite com guia; a Lei 5.504/1999 proibia cães em praias e a revogação não foi confirmada. Área cinzenta: não afirmar que é permitido.", "onde": "na praia"}',
     'Lei Municipal nº 9.108/2016, art. 10 × Lei Municipal nº 5.504/1999 (Salvador)',
     'https://leismunicipais.com.br/a1/ba/s/salvador/lei-ordinaria/2016/911/9108',
     DATE '2026-09-15',
     'baixa',
     'conflito',
     'plataforma',
     (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')),
    ('lr-ba-salvador-dejetos',
     'BA',
     'Salvador',
     'municipal',
     'dejetos',
     'location',
     '{"locais": ["qualquer local público"]}',
     '{"itens": ["recolher os dejetos"], "multa": "R$ 1.000 a R$ 5.000; reincidência até R$ 10.000", "onde": "em qualquer local público"}',
     'Lei Municipal nº 9.108/2016 (Salvador), arts. 7º e 32',
     'https://leismunicipais.com.br/a1/ba/s/salvador/lei-ordinaria/2016/911/9108',
     DATE '2026-09-15',
     'alta',
     'vigente',
     'plataforma',
     (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')),
    ('lr-pe-praia-coleira',
     'PE',
     NULL,
     'estadual',
     'praia',
     'location',
     '{"locais": ["faixa de praia"]}',
     '{"itens": ["coleira", "cão a no máximo 1 m de quem conduz"], "nota": "A lei menciona o tutor; aplicado a quem conduz o cão. Na prática, inviável com vários cães ao mesmo tempo.", "onde": "na faixa de praia"}',
     'Lei Estadual nº 12.321/2003, alterada pela Lei nº 17.924/2022 (PE), art. 4º',
     'https://legis.alepe.pe.gov.br/texto.aspx?id=67555&tipo=TEXTOORIGINAL',
     DATE '2026-09-15',
     'alta',
     'vigente',
     'plataforma',
     (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')),
    ('lr-pe-praia-dejetos',
     'PE',
     NULL,
     'estadual',
     'dejetos',
     'location',
     '{"locais": ["faixa de praia"]}',
     '{"itens": ["recolher os dejetos imediatamente"], "onde": "na faixa de praia"}',
     'Lei Estadual nº 12.321/2003, alterada pela Lei nº 17.924/2022 (PE), art. 4º, §3º',
     'https://legis.alepe.pe.gov.br/texto.aspx?id=67555&tipo=TEXTOORIGINAL',
     DATE '2026-09-15',
     'alta',
     'vigente',
     'plataforma',
     (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc'))
ON CONFLICT (id) DO NOTHING;

-- RLS: leitura liberada em qualquer escopo; escrita só no escopo global '*'.
ALTER TABLE local_rules ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS local_rules_read ON local_rules;
CREATE POLICY local_rules_read ON local_rules FOR SELECT USING (true);
DROP POLICY IF EXISTS local_rules_write_global ON local_rules;
CREATE POLICY local_rules_write_global ON local_rules FOR ALL
  USING ( current_setting('app.current_tenant', true) = '*' )
  WITH CHECK ( current_setting('app.current_tenant', true) = '*' );

-- O role da app precisa ler (escrita continua barrada pela policy fora do escopo '*') — padrão 0050/0053/0110.
GRANT SELECT, INSERT, UPDATE, DELETE ON local_rules TO aumigao_app;

UPDATE alembic_version SET version_num = '0111_local_rules_pet_reactive'
 WHERE version_num = '0110_walker_training';

COMMIT;

-- Conferência (rodar depois, fora da transação):
--   SELECT version_num FROM alembic_version;                                     -> 0111_local_rules_pet_reactive
--   SELECT id, status, confianca FROM local_rules ORDER BY id;                     -> 7 linhas (lr-ba-salvador-* x5, lr-pe-praia-* x2)
--   SELECT count(*) FROM local_rules WHERE tema = 'limite_caes';                   -> 0
--   SELECT policyname FROM pg_policies WHERE tablename = 'local_rules';            -> local_rules_read, local_rules_write_global
--   SELECT count(*) FROM pets WHERE is_reactive;                                   -> pets com chip "Reativo" (conferir com behavior_notes)
--   SELECT has_table_privilege('aumigao_app', 'local_rules', 'SELECT');            -> true
