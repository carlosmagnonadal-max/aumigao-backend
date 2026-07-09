"""Guarda: IDs de revision do alembic cabem em alembic_version (VARCHAR(32)).

Incidente 2026-07-03: a revision "0095_product_highlight_product_url" (34 chars)
estourou StringDataRightTruncation no CI rls-pg (banco criado do zero usa o
default VARCHAR(32) do alembic). Este teste lê os arquivos por regex — sem
importar alembic — para rodar em qualquer ambiente.
"""
import re
from pathlib import Path

VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"
_REVISION_RE = re.compile(r'^revision(?::\s*str)?\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
MAX_LEN = 32

# Revisions HISTÓRICAS acima de 32 chars, anteriores à guarda. Nunca serão head
# num setup fresco (o stamp só grava o head) e renomeá-las quebraria a cadeia
# down_revision. NÃO adicione entradas novas aqui — encurte o ID da migration.
_GRANDFATHERED = frozenset({
    "0050_walker_availability_exceptions",
    "0051_walker_exception_tenant_scope",
    "0057_tutor_subscription_credits_granted",
    "0066_walker_profile_suspension_audit",
    "0068_credit_ledger_cycle_reference",
    "0082_saas_subscription_unique_active",
    "0089_tutor_subscription_cancel_reason",
    # 0099 ja esta aplicada em prod (Neon) — renomear quebraria o historico.
    "0099_rls_support_tickets_user_own",
})


def test_new_revision_ids_fit_alembic_version_column():
    files = sorted(VERSIONS_DIR.glob("*.py"))
    assert files, f"nenhuma migration encontrada em {VERSIONS_DIR}"
    too_long = []
    for f in files:
        m = _REVISION_RE.search(f.read_text(encoding="utf-8"))
        if not m:
            continue
        rev = m.group(1)
        if len(rev) > MAX_LEN and rev not in _GRANDFATHERED:
            too_long.append(f"{f.name}: '{rev}' ({len(rev)} chars)")
    assert not too_long, (
        "Revision ID acima de 32 chars quebra o `alembic stamp head` do CI rls-pg "
        "(alembic_version.version_num = VARCHAR(32) em banco novo). Encurte o ID: "
        + "; ".join(too_long)
    )
