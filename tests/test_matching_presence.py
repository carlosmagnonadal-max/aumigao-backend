"""WK-10 — matching consome presença real (is_online).

availability_score deixa de ser constante (online > offline) e há um gate ligável
por flag (MATCHING_REQUIRE_ONLINE) que exclui o passeador offline do pool.
"""
import types

import pytest

from app.schemas.matching import MatchingWalkerRequest
from app.services import matching_service as ms


class _FakeProfile:
    def __init__(self, is_online):
        self.user_id = "w1"
        self.is_online = is_online


def _req():
    return MatchingWalkerRequest()  # sem scheduled_at -> base 80


def test_availability_score_is_not_constant_reflects_online(monkeypatch):
    # sem conflito de agenda
    monkeypatch.setattr(ms, "has_schedule_conflict", lambda *a, **k: False)
    online = ms.calculate_availability_score(_FakeProfile(True), _req(), db=None)
    offline = ms.calculate_availability_score(_FakeProfile(False), _req(), db=None)
    assert online > offline  # deixou de ser constante
    assert offline > 0  # offline ainda pontua (a menos que o gate exclua)


def test_schedule_conflict_still_zero(monkeypatch):
    monkeypatch.setattr(ms, "has_schedule_conflict", lambda *a, **k: True)
    assert ms.calculate_availability_score(_FakeProfile(True), _req(), db=None) == 0.0


def test_online_gate_off_by_default_lets_offline_pass(monkeypatch):
    monkeypatch.delenv("MATCHING_REQUIRE_ONLINE", raising=False)
    assert ms.passes_online_gate(_FakeProfile(True)) is True
    assert ms.passes_online_gate(_FakeProfile(False)) is True


def test_online_gate_on_excludes_offline(monkeypatch):
    monkeypatch.setenv("MATCHING_REQUIRE_ONLINE", "true")
    assert ms.passes_online_gate(_FakeProfile(True)) is True
    assert ms.passes_online_gate(_FakeProfile(False)) is False
