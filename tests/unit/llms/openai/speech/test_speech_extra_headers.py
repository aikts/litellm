"""
Regression tests for KTS fork: `extra_headers` passed via litellm_params
(e.g. from proxy config.yaml) must be forwarded as HTTP headers on OpenAI
text-to-speech requests.

Previously the openai branch of `litellm.speech()` never propagated
`extra_headers`, so custom auth headers (X-AgentPlatform-Proxy-Key) were
silently dropped.
"""

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI

import litellm

EXTRA_HEADERS = {"X-AgentPlatform-Proxy-Key": "secret-key"}


def _speech_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"AUDIO", headers={"content-type": "audio/mpeg"})


class TestSpeechExtraHeaders:
    def test_speech_forwards_extra_headers_as_http_headers(self):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _speech_response(request)

        client = OpenAI(
            api_key="sk-test",
            base_url="http://fake.local/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        litellm.speech(
            model="openai/tts-1",
            input="hello",
            voice="alloy",
            api_key="sk-test",
            api_base="http://fake.local/v1",
            extra_headers=EXTRA_HEADERS,
            client=client,
        )

        assert len(captured) == 1
        assert captured[0].headers.get("x-agentplatform-proxy-key") == "secret-key"

    @pytest.mark.asyncio
    async def test_aspeech_forwards_extra_headers_as_http_headers(self):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _speech_response(request)

        client = AsyncOpenAI(
            api_key="sk-test",
            base_url="http://fake.local/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        await litellm.aspeech(
            model="openai/tts-1",
            input="hello",
            voice="alloy",
            api_key="sk-test",
            api_base="http://fake.local/v1",
            extra_headers=EXTRA_HEADERS,
            client=client,
        )

        assert len(captured) == 1
        assert captured[0].headers.get("x-agentplatform-proxy-key") == "secret-key"
