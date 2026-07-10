"""Motor de cancelamento — migration 0107: colunas em walks/payments/tenant_settings
+ walk_completion_reviews (kind/compensation_amount).

Valida que:
- alembic resolve UM unico head e que a 0107 esta na cadeia;
- a revision id <= 32 chars;
- a 0107 encadeia na 0106_cost_alerts (head anterior);
- os modelos ORM refletem as novas colunas (schema fresco via metadata).
"""
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base

_REV = "0107_walk_cancellation"


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def test_single_head_and_0107_in_chain():
    script = _script()
    heads = list(script.get_heads())
    assert len(heads) == 1, heads
    chain = {rev.revision for rev in script.walk_revisions()}
    assert _REV in chain


def test_revision_id_within_32_chars():
    assert len(_REV) <= 32, len(_REV)


def test_0107_chains_on_0106():
    rev = _script().get_revision(_REV)
    assert rev.down_revision == "0106_cost_alerts"


def test_new_columns_present_in_orm_models():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    insp = inspect(engine)

    walk_cols = {c["name"] for c in insp.get_columns("walks")}
    assert {"cancellation_reason_type", "cancellation_reason", "cancelled_at", "cancelled_by_role"} <= walk_cols

    payment_cols = {c["name"] for c in insp.get_columns("payments")}
    assert {"refund_status", "refunded_amount"} <= payment_cols

    settings_cols = {c["name"] for c in insp.get_columns("tenant_settings")}
    assert {
        "cancellation_free_window_minutes",
        "late_cancellation_fee_percent",
        "late_fee_walker_share_percent",
        "auto_refund_on_cancel",
    } <= settings_cols

    review_cols = {c["name"] for c in insp.get_columns("walk_completion_reviews")}
    assert {"kind", "compensation_amount"} <= review_cols
