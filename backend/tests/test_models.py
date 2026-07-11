"""Model library + role assignments: CRUD, role resolution, capability
warnings, requeue-on-assign, and probe failure shapes."""

from datetime import datetime, timezone

import pytest

from app.agents.models import ProviderNotConfigured, get_provider, probe_endpoint, probe_transcription
from app.models import Bot, Chat, Message, MessageAttachment


VISION_MODEL = {
    "name": "qwen-vl",
    "base_url": "http://vllm:8000/v1",
    "model_name": "Qwen/Qwen3-VL-8B",
    "api_key": "sk-x",
    "capabilities": {"chat": True, "vision": True},
}
TEXT_MODEL = {
    "name": "llama",
    "base_url": "http://ollama:11434/v1",
    "model_name": "llama3",
    "capabilities": {"chat": True},
}


async def test_create_list_and_key_semantics(client):
    created = await client.post("/api/models", json=VISION_MODEL)
    assert created.status_code == 201
    body = created.json()
    assert body["has_api_key"] is True
    assert body["capabilities"]["vision"] is True
    assert body["assigned_roles"] == []

    # duplicate name rejected
    assert (await client.post("/api/models", json=VISION_MODEL)).status_code == 409

    # None keeps the key, "" clears it
    kept = await client.put(f"/api/models/{body['id']}", json={**VISION_MODEL, "api_key": None})
    assert kept.json()["has_api_key"] is True
    cleared = await client.put(f"/api/models/{body['id']}", json={**VISION_MODEL, "api_key": ""})
    assert cleared.json()["has_api_key"] is False

    listed = await client.get("/api/models")
    assert [m["name"] for m in listed.json()] == ["qwen-vl"]


async def test_role_assignment_resolves_get_provider(client, db_sessionmaker):
    model_id = (await client.post("/api/models", json=TEXT_MODEL)).json()["id"]
    assigned = await client.put("/api/model-roles/agent", json={"model_id": model_id})
    assert assigned.status_code == 200
    assert assigned.json()["capability_ok"] is True

    async with db_sessionmaker() as s:
        resolved = await get_provider(s, "agent")
        assert resolved.model_name == "llama3"
        with pytest.raises(ProviderNotConfigured):
            await get_provider(s, "intent")

    roles = {r["role"]: r for r in (await client.get("/api/model-roles")).json()}
    assert set(roles) == {"agent", "intent", "vision", "transcription", "video"}
    assert roles["agent"]["model_name"] == "llama"
    assert roles["intent"]["model_id"] is None

    # model list reflects the assignment
    listed = (await client.get("/api/models")).json()
    assert listed[0]["assigned_roles"] == ["agent"]


async def test_capability_warning_for_mismatched_role(client):
    model_id = (await client.post("/api/models", json=TEXT_MODEL)).json()["id"]
    assigned = await client.put("/api/model-roles/vision", json={"model_id": model_id})
    assert assigned.json()["capability_ok"] is False  # text model on a vision role

    assert (await client.put("/api/model-roles/nonsense", json={"model_id": model_id})).status_code == 422
    assert (await client.put("/api/model-roles/agent", json={"model_id": 12345})).status_code == 422


async def test_delete_assigned_model_conflicts(client):
    model_id = (await client.post("/api/models", json=TEXT_MODEL)).json()["id"]
    await client.put("/api/model-roles/agent", json={"model_id": model_id})

    resp = await client.delete(f"/api/models/{model_id}")
    assert resp.status_code == 409
    assert "agent" in resp.json()["detail"]

    assert (await client.delete("/api/model-roles/agent")).status_code == 204
    assert (await client.delete(f"/api/models/{model_id}")).status_code == 204
    assert (await client.delete("/api/model-roles/agent")).status_code == 404


async def test_assigning_media_role_requeues_skipped_attachments(client, db_sessionmaker):
    async with db_sessionmaker() as s:
        bot = Bot(tg_bot_id=1, username="b", name="b", token_encrypted="x",
                  can_read_all_group_messages=True)
        s.add(bot)
        await s.flush()
        chat = Chat(bot_id=bot.id, tg_chat_id=-100, type="supergroup", status="authorized")
        s.add(chat)
        await s.flush()
        msg = Message(chat_id=chat.id, tg_message_id=1, sender_name="A", text="",
                      sent_at=datetime.now(timezone.utc))
        msg.attachment = MessageAttachment(
            chat_id=chat.id, tg_message_id=1, kind="photo", file_id="f", file_unique_id="u",
            status="skipped", error="no vision model configured", attempts=1,
        )
        voice = Message(chat_id=chat.id, tg_message_id=2, sender_name="A", text="",
                        sent_at=datetime.now(timezone.utc))
        voice.attachment = MessageAttachment(
            chat_id=chat.id, tg_message_id=2, kind="voice", file_id="f2", file_unique_id="u2",
            status="skipped", error="no transcription model configured", attempts=1,
        )
        s.add_all([msg, voice])
        await s.commit()

    model_id = (await client.post("/api/models", json=VISION_MODEL)).json()["id"]
    await client.put("/api/model-roles/vision", json={"model_id": model_id})

    async with db_sessionmaker() as s:
        photo_att = (await s.get(Message, msg.id)).attachment
        assert photo_att.status == "pending"
        assert photo_att.attempts == 0 and photo_att.error is None
        voice_att = (await s.get(Message, voice.id)).attachment
        assert voice_att.status == "skipped"  # different role — untouched


async def test_probe_unreachable_endpoint_fails_with_direction():
    ok, detail = await probe_endpoint("http://127.0.0.1:9", "some-model", None)
    assert not ok
    assert "Couldn't reach" in detail
    assert "host.docker.internal" in detail


async def test_probe_transcription_unreachable_fails():
    ok, detail = await probe_transcription("http://127.0.0.1:9/v1", "whisper", None)
    assert not ok
    assert detail


async def test_evict_model_forces_fresh_client():
    from app.agents.models import build_model, evict_model
    from app.models import ConnectedModel

    provider = ConnectedModel(
        name="m", base_url="http://unused", model_name="test", api_key_encrypted=None
    )
    first = build_model(provider)
    assert build_model(provider) is first  # cached
    evict_model(provider)
    assert build_model(provider) is not first  # poisoned client replaced


async def test_role_reasoning_effort_saved_and_validated(db_sessionmaker, client, monkeypatch):
    """A reasoning level saves only if the live probe accepts it; Default
    (null) skips the probe entirely; a rejected level never persists."""
    import app.api.models as api_models
    from sqlalchemy import select

    from app.models import ConnectedModel, ModelRoleAssignment

    async with db_sessionmaker() as s:
        s.add(ConnectedModel(name="m", base_url="http://x", model_name="gpt-test",
                             capabilities={"chat": True}))
        await s.commit()
        model_id = (await s.execute(select(ConnectedModel))).scalar_one().id

    probes: list[str] = []

    async def fake_probe(provider, effort):
        probes.append(effort)
        return (effort != "xhigh"), f"probe({effort})"

    monkeypatch.setattr(api_models, "probe_reasoning", fake_probe)

    # Default: no probe, saves as NULL
    got = await client.put("/api/model-roles/agent", json={"model_id": model_id})
    assert got.status_code == 200 and got.json()["reasoning_effort"] is None
    assert probes == []

    # Supported level: probed once, persisted, surfaced in the list endpoint
    got = await client.put("/api/model-roles/agent",
                           json={"model_id": model_id, "reasoning_effort": "medium"})
    assert got.status_code == 200 and got.json()["reasoning_effort"] == "medium"
    assert probes == ["medium"]
    roles = {r["role"]: r for r in (await client.get("/api/model-roles")).json()}
    assert roles["agent"]["reasoning_effort"] == "medium"

    # Rejected level: 422, assignment keeps the previous value
    got = await client.put("/api/model-roles/agent",
                           json={"model_id": model_id, "reasoning_effort": "xhigh"})
    assert got.status_code == 422 and "xhigh" in got.json()["detail"]
    async with db_sessionmaker() as s:
        assert (await s.get(ModelRoleAssignment, "agent")).reasoning_effort == "medium"


def test_reasoning_settings_omits_default():
    from app.agents.models import reasoning_settings

    assert reasoning_settings(None) == {}
    assert reasoning_settings("") == {}
    assert reasoning_settings("high") == {"openai_reasoning_effort": "high"}


async def test_api_dialect_roundtrip_and_model_class(db_sessionmaker, client, monkeypatch):
    """The api field persists through the API, selects the pydantic-ai model
    class in build_model, and keys the cache (same endpoint on both dialects
    coexists)."""
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel

    import app.agents.models as am
    from app.models import ConnectedModel

    created = await client.post("/api/models", json={
        "name": "luna", "base_url": "http://x", "model_name": "gpt-luna",
        "api": "responses", "capabilities": {"chat": True},
    })
    assert created.status_code == 201 and created.json()["api"] == "responses"
    assert (await client.post("/api/models", json={
        "name": "bad", "base_url": "http://x", "model_name": "m", "api": "grpc",
    })).status_code == 422  # unknown dialect rejected at the boundary

    async with db_sessionmaker() as s:
        from sqlalchemy import select
        m = (await s.execute(select(ConnectedModel))).scalar_one()
        assert m.api == "responses"
        assert isinstance(am.build_model(m), OpenAIResponsesModel)
        m.api = "chat"
        assert isinstance(am.build_model(m), OpenAIChatModel)
        # distinct cache entries per dialect; evict removes the right one
        chat_model = am.build_model(m)
        m.api = "responses"
        responses_model = am.build_model(m)
        assert chat_model is not responses_model
        am.evict_model(m)
