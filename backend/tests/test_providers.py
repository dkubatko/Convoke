from app.agents.models import probe_endpoint


async def test_probe_unreachable_endpoint_fails_with_direction():
    ok, detail = await probe_endpoint("http://127.0.0.1:9", "some-model", None)
    assert not ok
    assert "Couldn't reach" in detail
    assert "host.docker.internal" in detail


async def test_probe_wrong_path_returns_endpoint_error():
    # httpbin-style: nothing OpenAI-compatible here — any response shape error
    # must come back as text, not raise.
    ok, detail = await probe_endpoint("http://127.0.0.1:9/v1", "m", "key")
    assert not ok
    assert detail
