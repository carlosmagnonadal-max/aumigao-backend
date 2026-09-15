"""S1 — push do Botão de Emergência.

walk_emergency (tutor) e walk_emergency_admin (admins do tenant) são críticos
(priority high) e ignoram o toggle push_notifications do plano do tenant:
segurança do passeio não é configurável por tenant (D3; desvio DV6 do plano).
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base
from app.models.notification import Notification
from app.models.push_token import PushToken
from app.models.tenant import Tenant
from app.services.push_notifications import (
    ANDROID_DEFAULT_CHANNEL,
    _build_push_messages,
    _push_notifications_enabled_for,
    _should_push,
)


def _notification(type_: str, tenant_id: str | None = None) -> Notification:
    return Notification(
        id="n-emg", tenant_id=tenant_id, user_id="tutor-1", user_role="tutor",
        title="Emergência no passeio de Rex", message="O passeador está ligando.",
        type=type_, related_entity_type="walk", related_entity_id="walk-1",
    )


def test_emergency_types_are_critical():
    assert _should_push(_notification("walk_emergency"))
    assert _should_push(_notification("walk_emergency_admin"))


def test_emergency_push_ignores_tenant_push_toggle(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id="t-emg", name="Tenant Emg", slug="t-emg"))
    db.commit()
    monkeypatch.setattr(
        "app.services.tenant_plan_service.tenant_feature_enabled",
        lambda tenant, session, key: False,
    )
    assert _push_notifications_enabled_for(db, _notification("walk_emergency", "t-emg")) is True
    assert _push_notifications_enabled_for(db, _notification("walk_emergency_admin", "t-emg")) is True
    assert _push_notifications_enabled_for(db, _notification("new_walk", "t-emg")) is False
    db.close()


def test_emergency_push_message_is_high_priority_and_points_to_walk():
    token = PushToken(id="tk", user_id="tutor-1", expo_push_token="ExponentPushToken[emg]", platform="android")
    messages = _build_push_messages(_notification("walk_emergency"), {"walk_id": "walk-1"}, [token])
    assert messages[0]["priority"] == "high"
    assert messages[0]["channelId"] == ANDROID_DEFAULT_CHANNEL
    assert messages[0]["data"]["related_entity_type"] == "walk"
    assert messages[0]["data"]["walk_id"] == "walk-1"
