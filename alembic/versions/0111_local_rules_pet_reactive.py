"""0111 — local_rules (regras locais estruturadas) + pets.is_reactive/reactivity_notes (S3).

Spec: docs/superpowers/specs/2026-09-15-seguranca-do-passeio-design.md (D6/D7, §3).
Fonte dos dados semeados (verificados em 15/09/2026):
  docs/manual-passeador/pesquisa-legislacao-local/RELATORIO-CONSOLIDADO-2026-09-15.md
  (A1 Salvador, A2 praia Salvador em conflito, A9 praia PE, A11 sem lei de passeador),
  NORDESTE.md (BA — Salvador; PE — Estado) e _parciais/NORDESTE-PE-CE.md.
Só achado [FATO] de confiança alta/média vira `vigente`. Praia de Salvador
(Lei 9.108/2016 art. 10 × Lei 5.504/1999, revogação não confirmada) entra como
`conflito` e NUNCA gera alerta categórico. Nenhum `limite_caes` (não há lei
vigente; PL 6.325/2025 é só projeto — D8).

(a) Tabela GLOBAL `local_rules` (sem tenant_id). RLS (Postgres): SELECT liberado
    em qualquer escopo (dado de referência pública lido sob a GUC do tenant do
    passeio); INSERT/UPDATE/DELETE só no escopo global '*'. Variante da 0080.
(b) pets.is_reactive (bool NOT NULL default false) + pets.reactivity_notes (TEXT NULL).
    Backfill: is_reactive = true onde o tutor já marcou o chip "Reativo"
    (behavior_notes contém "reativo").
(c) Seed idempotente por id fixo (padrão 0097).

Idempotente (padrão has_table/has_column 0107/0108). PG e SQLite.

Revision ID: 0111_local_rules_pet_reactive
Revises: 0110_walker_training
Create Date: 2026-09-15
"""
import json
import re
from datetime import date, datetime
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0111_local_rules_pet_reactive"
down_revision: Union[str, None] = "0110_walker_training"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "local_rules"
_READ_POLICY = "local_rules_read"
_WRITE_POLICY = "local_rules_write_global"
_GLOBAL_SCOPE = "current_setting('app.current_tenant', true) = '*'"
_INDEXES = (("ix_local_rules_uf", "uf"), ("ix_local_rules_tema", "tema"), ("ix_local_rules_status", "status"))

# S3-2: backfill via Python (não LIKE '%reativo%' puro) — exclui "não reativo"/
# "nao reativo" (negação) para não marcar como reativo quem foi explicitamente
# marcado como NÃO reativo em texto livre.
_REACTIVE_WORD_RE = re.compile(r"reativo", re.IGNORECASE)
_NEGATED_REACTIVE_RE = re.compile(r"n[aã]o\s+reativo", re.IGNORECASE)


def _note_marks_reactive(note) -> bool:
    text = note or ""
    if not _REACTIVE_WORD_RE.search(text):
        return False
    return bool(_REACTIVE_WORD_RE.search(_NEGATED_REACTIVE_RE.sub(" ", text)))


_VERIFIED = date(2026, 9, 15)
_LEI_SSA_9108_URL = "https://leismunicipais.com.br/a1/ba/s/salvador/lei-ordinaria/2016/911/9108"
_LEI_PE_PRAIA_URL = "https://legis.alepe.pe.gov.br/texto.aspx?id=67555&tipo=TEXTOORIGINAL"


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


_SSA = {"uf": "BA", "municipio": "Salvador", "nivel": "municipal", "verificado_em": _VERIFIED, "confirmado_por": "plataforma"}
_PE = {"uf": "PE", "municipio": None, "nivel": "estadual", "verificado_em": _VERIFIED, "confirmado_por": "plataforma"}

SEED_ROWS: list[dict] = [
    {
        **_SSA,
        "id": "lr-ba-salvador-focinheira-peso",
        "tema": "focinheira",
        "criterio": "weight_min_kg",
        # Lei: "acima de 24 kg" → comparação ESTRITA (inclusive=false): 24,0 kg não entra.
        "params_json": _json({"min_kg": 24, "inclusive": False, "size_fallback": ["grande", "gigante"]}),
        "exigencia_json": _json({
            "itens": ["guia", "focinheira"],
            "onde": "em local público ou privado de uso coletivo",
            "detalhe": "Cães de grande porte (acima de 24 kg) e de porte gigante, em ambiente público "
                       "ou privado de uso coletivo, sempre acompanhados do responsável.",
        }),
        "norma": "Lei Municipal nº 9.108/2016 (Salvador), arts. 5º e 11",
        "fonte_url": _LEI_SSA_9108_URL,
        "confianca": "alta",
        "status": "vigente",
    },
    {
        **_SSA,
        "id": "lr-ba-salvador-focinheira-reativo",
        "tema": "focinheira",
        "criterio": "reactive",
        "params_json": _json({}),
        "exigencia_json": _json({
            "itens": ["guia", "focinheira"],
            "onde": "em local público",
            "nota": "A lei usa o termo 'bravios' sem definição; aplicado a cães declarados reativos "
                    "(padrão protetivo).",
        }),
        "norma": "Lei Municipal nº 9.108/2016 (Salvador), art. 11",
        "fonte_url": _LEI_SSA_9108_URL,
        "confianca": "media",
        "status": "vigente",
    },
    {
        **_SSA,
        "id": "lr-ba-salvador-guia-afluxo",
        "tema": "guia",
        "criterio": "location",
        "params_json": _json({"locais": ["grande afluxo de pessoas"]}),
        "exigencia_json": _json({"itens": ["guia"], "onde": "em ambiente de grande afluxo de pessoas"}),
        "norma": "Lei Municipal nº 9.108/2016 (Salvador), art. 10",
        "fonte_url": _LEI_SSA_9108_URL,
        "confianca": "alta",
        "status": "vigente",
    },
    {
        **_SSA,
        "id": "lr-ba-salvador-praia",
        "tema": "praia",
        "criterio": "location",
        "params_json": _json({"locais": ["praia"]}),
        "exigencia_json": _json({
            "itens": ["guia"],
            "onde": "na praia",
            "nota": "A Lei 9.108/2016 (art. 10) permite com guia; a Lei 5.504/1999 proibia cães em praias "
                    "e a revogação não foi confirmada. Área cinzenta: não afirmar que é permitido.",
        }),
        "norma": "Lei Municipal nº 9.108/2016, art. 10 × Lei Municipal nº 5.504/1999 (Salvador)",
        "fonte_url": _LEI_SSA_9108_URL,
        "confianca": "baixa",
        "status": "conflito",
    },
    {
        **_SSA,
        "id": "lr-ba-salvador-dejetos",
        "tema": "dejetos",
        "criterio": "location",
        "params_json": _json({"locais": ["qualquer local público"]}),
        "exigencia_json": _json({
            "itens": ["recolher os dejetos"],
            "onde": "em qualquer local público",
            "multa": "R$ 1.000 a R$ 5.000; reincidência até R$ 10.000",
        }),
        "norma": "Lei Municipal nº 9.108/2016 (Salvador), arts. 7º e 32",
        "fonte_url": _LEI_SSA_9108_URL,
        "confianca": "alta",
        "status": "vigente",
    },
    {
        **_PE,
        "id": "lr-pe-praia-coleira",
        "tema": "praia",
        "criterio": "location",
        "params_json": _json({"locais": ["faixa de praia"]}),
        "exigencia_json": _json({
            "itens": ["coleira", "cão a no máximo 1 m de quem conduz"],
            "onde": "na faixa de praia",
            "nota": "A lei menciona o tutor; aplicado a quem conduz o cão. Na prática, inviável com "
                    "vários cães ao mesmo tempo.",
        }),
        "norma": "Lei Estadual nº 12.321/2003, alterada pela Lei nº 17.924/2022 (PE), art. 4º",
        "fonte_url": _LEI_PE_PRAIA_URL,
        "confianca": "alta",
        "status": "vigente",
    },
    {
        **_PE,
        "id": "lr-pe-praia-dejetos",
        "tema": "dejetos",
        "criterio": "location",
        "params_json": _json({"locais": ["faixa de praia"]}),
        "exigencia_json": _json({"itens": ["recolher os dejetos imediatamente"], "onde": "na faixa de praia"}),
        "norma": "Lei Estadual nº 12.321/2003, alterada pela Lei nº 17.924/2022 (PE), art. 4º, §3º",
        "fonte_url": _LEI_PE_PRAIA_URL,
        "confianca": "alta",
        "status": "vigente",
    },
]


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _inspector().has_table(name)


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    return column in {c["name"] for c in _inspector().get_columns(table)}


def _has_index(table: str, index_name: str) -> bool:
    if not _has_table(table):
        return False
    return index_name in {ix["name"] for ix in _inspector().get_indexes(table)}


def _local_rules_table():
    return sa.table(
        _TABLE,
        sa.column("id", sa.String),
        sa.column("uf", sa.String),
        sa.column("municipio", sa.String),
        sa.column("nivel", sa.String),
        sa.column("tema", sa.String),
        sa.column("criterio", sa.String),
        sa.column("params_json", sa.Text),
        sa.column("exigencia_json", sa.Text),
        sa.column("norma", sa.String),
        sa.column("fonte_url", sa.String),
        sa.column("verificado_em", sa.Date),
        sa.column("confianca", sa.String),
        sa.column("status", sa.String),
        sa.column("confirmado_por", sa.String),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )


def _seed(bind) -> None:
    existing = {row[0] for row in bind.execute(sa.text(f'SELECT id FROM "{_TABLE}"'))}
    now = datetime.utcnow()
    missing = [dict(row, created_at=now, updated_at=now) for row in SEED_ROWS if row["id"] not in existing]
    if missing:
        op.bulk_insert(_local_rules_table(), missing)


def _enable_rls(bind) -> None:
    bind.execute(sa.text(f'ALTER TABLE "{_TABLE}" ENABLE ROW LEVEL SECURITY'))
    bind.execute(sa.text(f'DROP POLICY IF EXISTS {_READ_POLICY} ON "{_TABLE}"'))
    bind.execute(sa.text(f'CREATE POLICY {_READ_POLICY} ON "{_TABLE}" FOR SELECT USING (true)'))
    bind.execute(sa.text(f'DROP POLICY IF EXISTS {_WRITE_POLICY} ON "{_TABLE}"'))
    bind.execute(sa.text(
        f'CREATE POLICY {_WRITE_POLICY} ON "{_TABLE}" FOR ALL '
        f"USING ({_GLOBAL_SCOPE}) WITH CHECK ({_GLOBAL_SCOPE})"
    ))


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        # Escopo global p/ o backfill em pets (RLS-ON). Owner já não é forçado
        # (0043 sem FORCE), isto é só defesa.
        bind.execute(sa.text("SELECT set_config('app.current_tenant', '*', true)"))

    if not _has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("uf", sa.String(2), nullable=False),
            sa.Column("municipio", sa.String(), nullable=True),
            sa.Column("nivel", sa.String(), nullable=False),
            sa.Column("tema", sa.String(), nullable=False),
            sa.Column("criterio", sa.String(), nullable=False),
            sa.Column("params_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("exigencia_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("norma", sa.String(), nullable=False),
            sa.Column("fonte_url", sa.String(), nullable=True),
            sa.Column("verificado_em", sa.Date(), nullable=True),
            sa.Column("confianca", sa.String(), nullable=False, server_default="media"),
            sa.Column("status", sa.String(), nullable=False, server_default="vigente"),
            sa.Column("confirmado_por", sa.String(), nullable=False, server_default="plataforma"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    for index_name, column in _INDEXES:
        if not _has_index(_TABLE, index_name):
            op.create_index(index_name, _TABLE, [column], unique=False)

    if not _has_column("pets", "is_reactive"):
        op.add_column("pets", sa.Column("is_reactive", sa.Boolean(), nullable=False, server_default=sa.false()))
    if not _has_column("pets", "reactivity_notes"):
        op.add_column("pets", sa.Column("reactivity_notes", sa.Text(), nullable=True))

    # S3-2: LIKE '%reativo%' puro marcaria também "não reativo"/"nao reativo" —
    # filtra em Python (regex com exclusão de negação) antes do UPDATE.
    candidates = bind.execute(
        sa.text("SELECT id, behavior_notes FROM pets WHERE is_reactive = :no"), {"no": False}
    ).fetchall()
    reactive_ids = [row[0] for row in candidates if _note_marks_reactive(row[1])]
    if reactive_ids:
        bind.execute(
            sa.text("UPDATE pets SET is_reactive = :yes WHERE id = :id"),
            [{"yes": True, "id": pet_id} for pet_id in reactive_ids],
        )

    _seed(bind)
    if is_pg:
        _enable_rls(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(_TABLE):
        if bind.dialect.name == "postgresql":
            bind.execute(sa.text(f'DROP POLICY IF EXISTS {_WRITE_POLICY} ON "{_TABLE}"'))
            bind.execute(sa.text(f'DROP POLICY IF EXISTS {_READ_POLICY} ON "{_TABLE}"'))
        for index_name, _column in _INDEXES:
            if _has_index(_TABLE, index_name):
                op.drop_index(index_name, table_name=_TABLE)
        op.drop_table(_TABLE)
    if _has_column("pets", "reactivity_notes"):
        op.drop_column("pets", "reactivity_notes")
    if _has_column("pets", "is_reactive"):
        op.drop_column("pets", "is_reactive")
