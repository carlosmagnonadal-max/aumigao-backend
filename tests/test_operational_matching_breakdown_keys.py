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
