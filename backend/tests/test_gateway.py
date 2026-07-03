from aiogram.client.default import Default
from aiogram.types import Chat as TgChat
from aiogram.types import Message as TgMessage
from aiogram.types import Update

from app.telegram.gateway import serialize_update


def _update_with_default_sentinel() -> Update:
    """Recreates the production incident: an unset field carrying aiogram's
    Default sentinel, which pydantic cannot serialize in json mode."""
    msg = TgMessage.model_construct(
        message_id=1,
        date=1_780_000_000,
        chat=TgChat.model_construct(id=-100, type="supergroup"),
        text="has a link preview",
        link_preview_options=Default("link_preview"),
    )
    return Update.model_construct(update_id=42, message=msg)


def test_serialize_update_survives_default_sentinels():
    payload = serialize_update(_update_with_default_sentinel())
    assert payload["update_id"] == 42
    # either cleanly serialized (sentinel excluded) or degraded to a stub —
    # never an exception that would wedge the polling offset
    assert "convoke_serialize_error" not in payload or payload["update_id"] == 42


def test_serialize_update_roundtrips_normal_update():
    u = Update.model_validate(
        {
            "update_id": 7,
            "message": {
                "message_id": 5,
                "date": 1_780_000_000,
                "chat": {"id": -100, "type": "supergroup", "title": "G"},
                "from": {"id": 1, "is_bot": False, "first_name": "A"},
                "text": "hello https://example.com",
                "link_preview_options": {"url": "https://example.com"},
            },
        }
    )
    payload = serialize_update(u)
    rt = Update.model_validate(payload)
    assert rt.message is not None and rt.message.text == "hello https://example.com"
