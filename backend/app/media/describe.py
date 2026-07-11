"""Model calls that turn media bytes into text (the only place bytes meet
models). Vision goes through pydantic-ai with BinaryContent; transcription
POSTs to the OpenAI-compatible /audio/transcriptions endpoint directly —
pydantic-ai has no transcription abstraction and none is needed."""

import asyncio
import base64
import logging
import shutil
import tempfile
from pathlib import Path

import httpx
from pydantic_ai import Agent, BinaryContent

from app.agents.models import build_model
from app.core.config import get_settings
from app.core.crypto import decrypt
from app.models import ConnectedModel

log = logging.getLogger("convoke.media")

DESCRIBE_TIMEOUT_S = 120

IMAGE_PROMPT = (
    "This image was sent in a group chat; your description becomes its searchable memory.\n"
    "Describe it in 1-3 sentences, LEADING with the subject — what it is about — and only "
    "then the medium: 'Movie card for Shrek (2001 comedy) — a Google-search screenshot; …', "
    "never 'A screenshot of …'. Quote any visible text VERBATIM — tickets, receipts, "
    "posters, screenshots (dates, times, names, amounts matter most).\n"
    "Keep it under {max_chars} characters. Reply with the description only."
)

VIDEO_PROMPT = (
    "This video was sent in a group chat; your description becomes its searchable memory.\n"
    "Describe it in 1-3 sentences, LEADING with the subject — what it is about — then the "
    "format. Quote any visible text VERBATIM.\n"
    "Keep it under {max_chars} characters. Reply with the description only."
)

FRAMES_PROMPT = (
    "These images are frames sampled from a video sent in a group chat (the first is its "
    "thumbnail); your description becomes the video's searchable memory.\n"
    "Describe the video in 1-3 sentences, LEADING with the subject — what it is about — "
    "then the format. Quote any visible text VERBATIM.{transcript_line}\n"
    "Keep it under {max_chars} characters. Reply with the description only."
)

# Question-directed re-inspection (the agent's inspect_media tool): unlike the
# describe prompts, the output is an ANSWER, not an index entry.
INSPECT_IMAGE_PROMPT = (
    "This image was sent in a group chat. Answer the question about it precisely; quote "
    "any relevant visible text VERBATIM (dates, times, names, amounts matter most). If "
    "the image cannot answer the question, say what it shows instead.\n"
    'Question: "{question}"'
)

INSPECT_FRAMES_PROMPT = (
    "These images are frames sampled from a video sent in a group chat (the first is its "
    "thumbnail).{transcript_line}\n"
    "Answer the question about the video precisely; quote any relevant visible text or "
    "speech VERBATIM. If the frames cannot answer it, say what the video shows instead.\n"
    'Question: "{question}"'
)

INSPECT_MAX_CHARS = 1500  # answers return to the agent's context — keep them bounded


class Describer:
    """The media loop's model seam; tests substitute a fake."""

    def __init__(self) -> None:
        self.settings = get_settings()

    async def describe_image(
        self, provider: ConnectedModel, data: bytes, mime: str, caption: str = ""
    ) -> str:
        prompt = IMAGE_PROMPT.format(max_chars=self.settings.media_description_max_chars)
        if caption:
            prompt += f'\nThe sender captioned it: "{caption[:200]}"'
        agent = Agent(build_model(provider), model_settings={"max_tokens": 300})
        result = await asyncio.wait_for(
            agent.run([prompt, BinaryContent(data, media_type=mime)]), DESCRIBE_TIMEOUT_S
        )
        return (result.output or "").strip()[: self.settings.media_description_max_chars]

    async def describe_frames(
        self,
        provider: ConnectedModel,
        frames: list[bytes],
        caption: str = "",
        transcript: str | None = None,
    ) -> str:
        """Fallback video description: thumbnail + sampled frames as a
        multi-image vision call, composed with the audio transcript."""
        transcript_line = f'\nThe audio says: "{transcript[:500]}"' if transcript else ""
        prompt = FRAMES_PROMPT.format(
            transcript_line=transcript_line,
            max_chars=self.settings.media_description_max_chars,
        )
        if caption:
            prompt += f'\nThe sender captioned it: "{caption[:200]}"'
        agent = Agent(build_model(provider), model_settings={"max_tokens": 300})
        parts = [prompt, *(BinaryContent(f, media_type="image/jpeg") for f in frames)]
        result = await asyncio.wait_for(agent.run(parts), DESCRIBE_TIMEOUT_S)
        return (result.output or "").strip()[: self.settings.media_description_max_chars]

    async def describe_video_native(
        self, provider: ConnectedModel, data: bytes, mime: str, caption: str = ""
    ) -> str:
        """Video-native path (behind the `video` role): vLLM-convention
        video_url content part on a raw chat/completions call — pydantic-ai's
        OpenAI model doesn't emit video parts, so this bypasses it. Always
        chat/completions regardless of the model's `api` dialect: video_url
        is a vLLM convention with no Responses-API equivalent."""
        prompt = VIDEO_PROMPT.format(max_chars=self.settings.media_description_max_chars)
        if caption:
            prompt += f'\nThe sender captioned it: "{caption[:200]}"'
        api_key = decrypt(provider.api_key_encrypted) if provider.api_key_encrypted else None
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        b64 = base64.b64encode(data).decode()
        payload = {
            "model": provider.model_name,
            "max_tokens": 300,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "video_url", "video_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }
            ],
        }
        async with httpx.AsyncClient(timeout=DESCRIBE_TIMEOUT_S) as client:
            resp = await client.post(
                f"{provider.base_url.rstrip('/')}/chat/completions", headers=headers, json=payload
            )
        resp.raise_for_status()
        text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
        return text[: self.settings.media_description_max_chars]

    async def answer_about_image(
        self, provider: ConnectedModel, data: bytes, mime: str, question: str, caption: str = ""
    ) -> str:
        prompt = INSPECT_IMAGE_PROMPT.format(question=question[:500])
        if caption:
            prompt += f'\nThe sender captioned it: "{caption[:200]}"'
        agent = Agent(build_model(provider), model_settings={"max_tokens": 700})
        result = await asyncio.wait_for(
            agent.run([prompt, BinaryContent(data, media_type=mime)]), DESCRIBE_TIMEOUT_S
        )
        return (result.output or "").strip()[:INSPECT_MAX_CHARS]

    async def answer_about_frames(
        self,
        provider: ConnectedModel,
        frames: list[bytes],
        question: str,
        transcript: str | None = None,
        caption: str = "",
    ) -> str:
        transcript_line = f'\nThe audio says: "{transcript[:800]}"' if transcript else ""
        prompt = INSPECT_FRAMES_PROMPT.format(
            transcript_line=transcript_line, question=question[:500]
        )
        if caption:
            prompt += f'\nThe sender captioned it: "{caption[:200]}"'
        agent = Agent(build_model(provider), model_settings={"max_tokens": 700})
        parts = [prompt, *(BinaryContent(f, media_type="image/jpeg") for f in frames)]
        result = await asyncio.wait_for(agent.run(parts), DESCRIBE_TIMEOUT_S)
        return (result.output or "").strip()[:INSPECT_MAX_CHARS]

    async def sample_frames(self, data: bytes, count: int, duration_s: int | None) -> list[bytes]:
        """Evenly sample up to `count` JPEG frames via ffmpeg. Best-effort:
        no ffmpeg (or a decode failure) returns [] and the caller degrades to
        thumbnail-only."""
        if count <= 0 or not shutil.which("ffmpeg"):
            return []
        fps = count / max(duration_s or 10, 1)
        try:
            with tempfile.TemporaryDirectory(prefix="convoke-media-") as tmp:
                src = Path(tmp) / "in.bin"
                src.write_bytes(data)
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", str(src),
                    "-vf", f"fps={fps:.6f}", "-frames:v", str(count),
                    str(Path(tmp) / "frame_%02d.jpg"),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), 60)
                return [p.read_bytes() for p in sorted(Path(tmp).glob("frame_*.jpg"))]
        except Exception:  # noqa: BLE001 — frames are a bonus, never an error
            log.warning("frame sampling failed; describing from thumbnail only", exc_info=True)
            return []

    async def transcribe(
        self, provider: ConnectedModel, data: bytes, filename: str, mime: str
    ) -> str:
        api_key = decrypt(provider.api_key_encrypted) if provider.api_key_encrypted else None
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(timeout=DESCRIBE_TIMEOUT_S) as client:
            resp = await client.post(
                f"{provider.base_url.rstrip('/')}/audio/transcriptions",
                headers=headers,
                data={"model": provider.model_name, "response_format": "json"},
                files={"file": (filename, data, mime)},
            )
        resp.raise_for_status()
        return (resp.json().get("text") or "").strip()
