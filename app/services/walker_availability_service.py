"""Resolução de disponibilidade do passeador: recorrente + exceções por data.

Lógica de prioridade (da mais alta p/ mais baixa):
  1. Exceção kind="block" cobrindo o horário → INDISPONÍVEL (precede tudo).
  2. Exceção kind="open"  cobrindo o horário → DISPONÍVEL (adiciona janela extra).
  3. Disponibilidade recorrente (schedule_json) → DISPONÍVEL se o dia-da-semana
     estiver enabled=True e o horário cair dentro de algum slot configurado.
  4. Nenhuma regra aplicada → INDISPONÍVEL (conservador).

Formato do schedule_json (WalkerAvailability):
  {
    "Seg": {"enabled": true,  "slots": ["09:00", "15:00"]},
    "Ter": {"enabled": false, "slots": []},
    ...
    "Dom": {"enabled": true,  "slots": ["08:00"]}
  }

Chaves dos dias (seguem o frontend/hook useWalkerAvailability):
  0=Seg  1=Ter  2=Qua  3=Qui  4=Sex  5=Sáb  6=Dom
  (mapeado de datetime.weekday())

Semântica de slot recorrente:
  Cada slot "HH:MM" cobre uma janela de 1 hora: [HH:MM, HH:MM+1h).
  Ex.: slot "09:00" cobre 09:00 ≤ hhmm < 10:00.
  Isso reflete o modelo da UI, onde cada slot representa um bloco de 1h.

Semântica de faixa em exceções (start_time/end_time):
  Faixa contínua: start_time ≤ hhmm < end_time.
  NULL+NULL = dia inteiro bloqueado/aberto.
"""
import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.walker_availability import WalkerAvailability
from app.models.walker_availability_exception import WalkerAvailabilityException

# Mapeamento weekday (0=Segunda..6=Domingo) → chave do schedule_json.
_WEEKDAY_TO_KEY = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]


def _hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _covers(exc: WalkerAvailabilityException, hhmm: str) -> bool:
    """Retorna True se a exceção exc cobre o horário hhmm (HH:MM)."""
    if exc.start_time is None and exc.end_time is None:
        return True  # dia inteiro
    start = exc.start_time or "00:00"
    end = exc.end_time or "23:59"
    return start <= hhmm < end


def _exceptions_on(
    db: Session, walker_id: str, dt: datetime, tenant_id: str | None = None
) -> list[WalkerAvailabilityException]:
    q = db.query(WalkerAvailabilityException).filter(
        WalkerAvailabilityException.walker_user_id == walker_id,
        WalkerAvailabilityException.exception_date == dt.date(),
    )
    if tenant_id is not None:
        q = q.filter(
            (WalkerAvailabilityException.tenant_id.is_(None))
            | (WalkerAvailabilityException.tenant_id == tenant_id)
        )
    else:
        q = q.filter(WalkerAvailabilityException.tenant_id.is_(None))
    return q.all()


def _slot_covers(slot: str, hhmm: str) -> bool:
    """Slot recorrente "HH:MM" cobre [HH:MM, HH:MM + 1h).

    Slots são strings "HH:MM"; a aritmética é feita em minutos totais para
    evitar parsing de objetos time desnecessário.
    """
    def _to_minutes(s: str) -> int:
        h, m = s.split(":")
        return int(h) * 60 + int(m)

    slot_start = _to_minutes(slot)
    slot_end = slot_start + 60
    req = _to_minutes(hhmm)
    return slot_start <= req < slot_end


def _recurring_allows(db: Session, walker_id: str, dt: datetime) -> bool:
    """Consulta a disponibilidade recorrente (schedule_json) do passeador.

    Retorna True se:
      - Existe uma linha WalkerAvailability para walker_id.
      - O dia-da-semana de `dt` está enabled=True.
      - Algum slot configurado naquele dia cobre o horário de `dt`
        (semântica: slot "HH:MM" → janela de 1 hora [HH:MM, HH:MM+1h)).

    Retorna False (conservador) em todos os outros casos.
    """
    row = (
        db.query(WalkerAvailability)
        .filter(WalkerAvailability.walker_user_id == walker_id)
        .first()
    )
    if row is None or not row.schedule_json:
        return False

    try:
        schedule: dict = json.loads(row.schedule_json)
    except (ValueError, TypeError):
        return False

    day_key = _WEEKDAY_TO_KEY[dt.weekday()]
    day_cfg = schedule.get(day_key)
    if not day_cfg or not day_cfg.get("enabled", False):
        return False

    slots: list[str] = day_cfg.get("slots", [])
    hhmm = _hhmm(dt)
    return any(_slot_covers(slot, hhmm) for slot in slots)


def is_walker_available_at(
    db: Session, walker_id: str, dt: datetime, tenant_id: str | None = None
) -> bool:
    """Disponível no instante dt (opcionalmente no escopo de um tenant).

    Considera exceções globais (tenant_id IS NULL) e, se tenant_id informado,
    também as daquele tenant. Sem tenant_id = comportamento legado (só globais).

    Regras (em ordem de prioridade):
      1. Exceção block cobrindo dt.hour → False.
      2. Exceção open  cobrindo dt.hour → True.
      3. Recorrente (schedule_json) permite → True.
      4. Default conservador → False.
    """
    hhmm = _hhmm(dt)
    excs = _exceptions_on(db, walker_id, dt, tenant_id)

    if any(e.kind == "block" and _covers(e, hhmm) for e in excs):
        return False
    if any(e.kind == "open" and _covers(e, hhmm) for e in excs):
        return True

    return _recurring_allows(db, walker_id, dt)
