from __future__ import annotations

import app.models  # noqa: F401

from app.routes.tutor import TutorJoinRequest


def test_join_request_accepts_referral_code():
    req = TutorJoinRequest(tenant_slug="t1", referral_code="TUT-ABC-123")
    assert req.referral_code == "TUT-ABC-123"


def test_join_request_referral_code_optional():
    req = TutorJoinRequest(tenant_slug="t1")
    assert req.referral_code is None
