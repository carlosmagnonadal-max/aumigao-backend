"""S1 — regras do evento operacional emergency_call.

- Tem label próprio (aparece na timeline do tutor/admin).
- NUNCA derruba o score operacional do passeador: acionar emergência é
  comportamento desejado, apesar de severity="high" (desvio DV5 do plano).
"""
from types import SimpleNamespace

from app.services import operational_reliability_service as rel
from app.services import walker_operational_score_service as score


def _score(events):
    return score._score_from_inputs(
        completed_count=5, rating_avg=4.8, rating_count=5, events=events, rejected_count=0
    )


def test_emergency_call_constant_and_label():
    assert rel.EMERGENCY_CALL == "emergency_call"
    assert rel.EVENT_LABELS[rel.EMERGENCY_CALL] == "Emergência acionada pelo passeador"


def test_emergency_call_never_penalizes_walker_score():
    base = _score([])
    with_emergency = _score([SimpleNamespace(event_type="emergency_call", severity="high")])
    assert with_emergency["operational_score"] == base["operational_score"]
    assert with_emergency["score_details"]["high_attention_events"] == 0


def test_other_high_severity_events_still_penalize():
    base = _score([])
    no_show = _score([SimpleNamespace(event_type="walker_no_show", severity="high")])
    assert no_show["operational_score"] < base["operational_score"]
