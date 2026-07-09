"""Alerta sonoro nível 1 (08/07): pushes críticos saem com priority=high e
channelId — solicitações de passeio no canal Android dedicado walk-requests.

09/07: envio bem-sucedido passa a ser LOGADO (INFO) — sem isso o diagnóstico
"o push saiu ou não?" em produção era impossível (só falha era logada)."""
import io
import json
import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  — registra todas as tabelas no Base.metadata
from app.core.database import Base
from app.models.notification import Notification
from app.models.push_token import PushToken
from app.services.push_notifications import (
    ANDROID_DEFAULT_CHANNEL,
    ANDROID_WALK_REQUEST_CHANNEL,
    _build_push_messages,
    send_push_for_notification,
)


def _notification(type_: str) -> Notification:
    return Notification(
        id="n1", user_id="u1", user_role="walker", title="Nova solicitação",
        message="Passeio disponível", type=type_,
        related_entity_type="walk", related_entity_id="w1",
    )


def _token() -> PushToken:
    return PushToken(id="t1", user_id="u1", expo_push_token="ExponentPushToken[abc]", platform="android")


def test_walk_request_uses_dedicated_channel_and_high_priority():
    messages = _build_push_messages(_notification("walker_attempt_created"), {}, [_token()])
    assert len(messages) == 1
    msg = messages[0]
    assert msg["priority"] == "high"
    assert msg["channelId"] == ANDROID_WALK_REQUEST_CHANNEL
    assert msg["sound"] == "default"


def test_other_critical_types_use_default_channel():
    messages = _build_push_messages(_notification("payment_confirmed"), {}, [_token()])
    assert messages[0]["channelId"] == ANDROID_DEFAULT_CHANNEL
    assert messages[0]["priority"] == "high"


def test_metadata_merged_into_data():
    messages = _build_push_messages(_notification("new_walk"), {"walk_id": "w1"}, [_token()])
    assert messages[0]["data"]["walk_id"] == "w1"
    assert messages[0]["channelId"] == ANDROID_WALK_REQUEST_CHANNEL


def test_successful_send_emits_info_log(monkeypatch, caplog):
    """Envio aceito pelo Expo gera log INFO com notification_id, tipo e nº de tokens."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    notification = _notification("new_walk")
    db.add(notification)
    db.add(_token())
    db.commit()

    class _FakeResponse(io.BytesIO):
        def __init__(self):
            super().__init__(json.dumps({"data": [{"status": "ok"}]}).encode("utf-8"))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        "app.services.push_notifications.urllib.request.urlopen",
        lambda request, timeout: _FakeResponse(),
    )

    with caplog.at_level(logging.INFO, logger="aumigao.push_notifications"):
        send_push_for_notification(db, notification)

    assert "push notification sent" in caplog.text
    assert "notification_id=n1" in caplog.text
    assert "type=new_walk" in caplog.text
    assert "tokens=1" in caplog.text
