"""The chat media-status endpoint: description backlog counts by status."""

from sqlalchemy import select

from app.models import Chat, Message, MessageAttachment

from tests.test_media_loop import T0, seed


async def test_media_status_endpoint_counts_by_status(db_sessionmaker, client):
    await seed(db_sessionmaker)  # one pending photo (tg 20)
    async with db_sessionmaker() as s:
        chat_id = (await s.execute(select(Chat.id))).scalar_one()
        done = Message(chat_id=chat_id, tg_message_id=21, sender_name="A", text="",
                       sent_at=T0)
        done.attachment = MessageAttachment(
            chat_id=chat_id, tg_message_id=21, kind="voice", file_id="f2",
            file_unique_id="u2", status="described", transcript="hi",
        )
        s.add(done)
        await s.commit()

    resp = await client.get(f"/api/chats/{chat_id}/media-status")
    assert resp.status_code == 200
    assert resp.json() == {"pending": 1, "described": 1, "failed": 0, "skipped": 0}
