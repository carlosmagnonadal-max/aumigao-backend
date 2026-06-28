"""Tarefa D (higiene): as chaves do score_breakdown salvo precisam ser honestas.

Antes: pet_size_experience / pet_behavior_experience (enganosas — na verdade
são experience_score / behavior_score genéricos, não leem o pet).
Agora: experience_score / behavior_score.

Testa _create_attempt isolando os efeitos colaterais (json dump, log, notify)
por monkeypatch, capturando o dict do breakdown antes de ser serializado.
"""
from app.services import operational_matching_service as oms
from app.models.walk import Walk


def test_score_breakdown_uses_honest_keys(monkeypatch):
    captured = {}

    def _capture_json_dump(value):
        # O breakdown é o único dict com a chave "rating" passado ao _json_dump aqui.
        if isinstance(value, dict) and "rating" in value:
            captured["breakdown"] = value
        return "{}"

    monkeypatch.setattr(oms, "_json_dump", _capture_json_dump)
    monkeypatch.setattr(oms, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(oms, "notify_walker_walk_event", lambda *a, **k: None)
    monkeypatch.setattr(oms, "notify_tutor_walk_event", lambda *a, **k: None)

    class _FakeDB:
        def add(self, *a, **k):
            pass

        def flush(self, *a, **k):
            pass

    walk = Walk(
        id="walk-d",
        tutor_id="tutor-d",
        pet_id="pet-d",
        scheduled_date="2024-05-10T14:00:00",
        duration_minutes=45,
        price=50.0,
        status="Agendado",
    )
    candidate = {
        "walker_id": "walker-d",
        "final_matching_score": 80.0,
        "proximity_score": 90,
        "rating_score": 88,
        "experience_score": 55,
        "behavior_score": 70,
        "boost_score": 0,
        "level": "Prata",
    }

    oms._create_attempt(_FakeDB(), walk, candidate, attempt_number=1)

    breakdown = captured["breakdown"]
    # Chaves novas, honestas:
    assert breakdown["experience_score"] == 55
    assert breakdown["behavior_score"] == 70
    # Chaves enganosas antigas NÃO existem mais:
    assert "pet_size_experience" not in breakdown
    assert "pet_behavior_experience" not in breakdown


# ---------------------------------------------------------------------------
# Item 1 — Linguagem neutra de marketplace (risco trabalhista)
# A mensagem de nova solicitação NÃO deve conter termos coercitivos.
# ---------------------------------------------------------------------------

def test_new_walk_notification_message_is_neutral(monkeypatch):
    """A notificação de nova oferta ao passeador não pode conter linguagem
    que vincule comportamento (aceite/recusa) a punição de score ou prazo
    obrigatório — isso configuraria subordinação algorítmica."""
    captured_notifications = []

    def _capture_notify(db, walk, walker_id, *, title, message, **kwargs):
        captured_notifications.append({"title": title, "message": message})

    monkeypatch.setattr(oms, "_json_dump", lambda v: "{}")
    monkeypatch.setattr(oms, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(oms, "notify_walker_walk_event", _capture_notify)
    monkeypatch.setattr(oms, "notify_tutor_walk_event", lambda *a, **k: None)

    class _FakeDB:
        def add(self, *a, **k):
            pass
        def flush(self, *a, **k):
            pass

    walk = Walk(
        id="walk-neutral",
        tutor_id="tutor-n",
        pet_id="pet-n",
        scheduled_date="2024-05-10T14:00:00",
        duration_minutes=30,
        price=40.0,
        status="Agendado",
    )
    candidate = {
        "walker_id": "walker-n",
        "final_matching_score": 75.0,
        "proximity_score": 85,
        "rating_score": 80,
        "experience_score": 60,
        "behavior_score": 65,
        "boost_score": 0,
        "level": "Bronze",
    }

    oms._create_attempt(_FakeDB(), walk, candidate, attempt_number=1)

    assert captured_notifications, "notify_walker_walk_event deve ser chamada"
    msg = captured_notifications[0]["message"].lower()

    # Termos coercitivos proibidos na mensagem ao passeador:
    FORBIDDEN_TERMS = ["pontuação", "pontuacao", "penaliz", "manter sua", "obrigat"]
    for term in FORBIDDEN_TERMS:
        assert term not in msg, (
            f"Mensagem ao passeador contém termo coercitivo '{term}': {captured_notifications[0]['message']!r}"
        )
