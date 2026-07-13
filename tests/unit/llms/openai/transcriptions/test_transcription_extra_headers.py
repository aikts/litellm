"""
Regression tests for KTS fork: `extra_headers` passed via litellm_params
(e.g. from proxy config.yaml) must be forwarded as HTTP headers on OpenAI
audio transcription requests.

Previously `litellm.transcription()` left `extra_headers` in kwargs, so it
leaked into `optional_params["extra_body"]` via
add_provider_specific_params_to_optional_params and was serialized into the
multipart form body (`extra_headers[X-AgentPlatform-Proxy-Key]=...`) instead
of being sent as an HTTP header — breaking the request AND leaking the
secret to the upstream provider.
"""

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI

import litellm

EXTRA_HEADERS = {"X-AgentPlatform-Proxy-Key": "secret-key"}
AUDIO_FILE = ("audio.mp3", b"\xff\xfb\x90\x00" + b"\x00" * 100)


def _transcription_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"text": "hi", "duration": 1.0, "language": "en"})


def _assert_header_sent_and_not_leaked(request: httpx.Request) -> None:
    assert request.headers.get("x-agentplatform-proxy-key") == "secret-key"
    assert b"extra_headers" not in request.content
    assert b"secret-key" not in request.content


class TestTranscriptionExtraHeaders:
    def test_transcription_forwards_extra_headers_as_http_headers(self):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _transcription_response(request)

        client = OpenAI(
            api_key="sk-test",
            base_url="http://fake.local/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        litellm.transcription(
            model="openai/whisper-1",
            file=AUDIO_FILE,
            api_key="sk-test",
            api_base="http://fake.local/v1",
            extra_headers=EXTRA_HEADERS,
            client=client,
        )

        assert len(captured) == 1
        _assert_header_sent_and_not_leaked(captured[0])

    @pytest.mark.asyncio
    async def test_atranscription_forwards_extra_headers_as_http_headers(self):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _transcription_response(request)

        client = AsyncOpenAI(
            api_key="sk-test",
            base_url="http://fake.local/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        await litellm.atranscription(
            model="openai/whisper-1",
            file=AUDIO_FILE,
            api_key="sk-test",
            api_base="http://fake.local/v1",
            extra_headers=EXTRA_HEADERS,
            client=client,
        )

        assert len(captured) == 1
        _assert_header_sent_and_not_leaked(captured[0])
