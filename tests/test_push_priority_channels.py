"""Alerta sonoro nível 1 (08/07): pushes críticos saem com priority=high e
channelId — solicitações de passeio no canal Android dedicado walk-requests."""
from app.models.notification import Notification
from app.models.push_token import PushToken
from app.services.push_notifications import (
    ANDROID_DEFAULT_CHANNEL,
    ANDROID_WALK_REQUEST_CHANNEL,
    _build_push_messages,
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
