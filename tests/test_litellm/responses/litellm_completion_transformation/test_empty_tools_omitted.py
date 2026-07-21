"""
Tests that the Responses -> Chat Completions bridge omits `tools` entirely when the
Responses API request carries none.

Some OpenAI-compatible upstreams (Cloud.ru's foundation-models API, Yandex) reject
`"tools": []` with a 400 instead of treating it as "no tools", which made every
bridged no-tool request fail.
"""

from litellm.responses.litellm_completion_transformation.transformation import (
    LiteLLMCompletionResponsesConfig,
)


def test_transform_request__no_tools__tools_key_omitted():
    result = LiteLLMCompletionResponsesConfig.transform_responses_api_request_to_chat_completion_request(
        model="openai/some-model",
        input="hello",
        responses_api_request={},
    )

    assert "tools" not in result


def test_transform_request__empty_tools_list__tools_key_omitted():
    result = LiteLLMCompletionResponsesConfig.transform_responses_api_request_to_chat_completion_request(
        model="openai/some-model",
        input="hello",
        responses_api_request={"tools": []},
    )

    assert "tools" not in result


def test_transform_request__tools_present__tools_forwarded():
    tools = [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Get the weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]

    result = LiteLLMCompletionResponsesConfig.transform_responses_api_request_to_chat_completion_request(
        model="openai/some-model",
        input="what is the weather in Moscow?",
        responses_api_request={"tools": tools},
    )

    assert len(result["tools"]) == 1
    assert result["tools"][0]["function"]["name"] == "get_weather"
