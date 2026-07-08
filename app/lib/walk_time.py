"""Conversão do horário do passeio (hora de parede LOCAL do tenant) para UTC.

`walks.scheduled_date` guarda 'YYYY-MM-DDTHH:MM[:SS]' na hora LOCAL do tenant —
o app grava o que o tutor escolheu no relógio dele. Comparar essa string direto
com datetime.utcnow() desloca tudo em -3h (America/Bahia): foi o bug que fez o
corte de 45min cancelar um passeio 1 minuto após a criação (08/07/2026) e
deletar a cobrança PIX no Asaas antes do tutor pagar.

Use walk_start_utc() em QUALQUER comparação de scheduled_date com relógio UTC.
Strings com offset explícito (Z/±HH:MM) são honradas; strings naive são
interpretadas na timezone do tenant.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

DEFAULT_TENANT_TZ = "America/Bahia"


def _safe_zone(tz_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or DEFAULT_TENANT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TENANT_TZ)


def parse_wall_time(value: str | None) -> datetime | None:
    """Parseia scheduled_date SEM interpretar timezone (pode voltar aware se a
    string tiver offset). Fallback: só a parte da data (início de dia)."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        date_part = raw.partition("T")[0]
        try:
            return datetime.fromisoformat(date_part)
        except ValueError:
            return None


def walk_start_utc(value: str | None, tz_name: str | None = None) -> datetime | None:
    """INÍCIO do passeio como datetime naive em UTC (comparável a utcnow()).

    - string aware (offset explícito): converte direto pra UTC.
    - string naive (caso padrão do app): interpreta na tz do tenant e converte.
    """
    parsed = parse_wall_time(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_safe_zone(tz_name))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def tenant_tz_name(db: Session, tenant_id: str | None) -> str:
    """Timezone configurada do tenant (tenant_settings.timezone); fallback padrão BR."""
    if not tenant_id:
        return DEFAULT_TENANT_TZ
    try:
        from app.models.tenant import TenantSettings

        row = (
            db.query(TenantSettings.timezone)
            .filter(TenantSettings.tenant_id == tenant_id)
            .first()
        )
        return (row[0] or DEFAULT_TENANT_TZ) if row else DEFAULT_TENANT_TZ
    except Exception:
        return DEFAULT_TENANT_TZ
