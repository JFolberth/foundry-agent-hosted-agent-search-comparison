import json

import httpx2
import pytest
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.core.credentials import AccessToken

from comparison.config import load_config, model_options, native_search_tool, prompt_definition


class Credential:
    async def get_token(self, *scopes, **kwargs):
        return AccessToken("test-only-not-a-real-token", 9999999999)

    async def close(self):
        pass


def test_native_prompt_and_hosted_parity(settings):
    config = load_config()
    prompt = prompt_definition(settings, config)
    assert isinstance(prompt, PromptAgentDefinition)
    data = prompt.as_dict()
    options = model_options(settings, config)
    assert data["tools"] == options["tools"] == [native_search_tool(settings, config).as_dict()]
    assert options["reasoning"] == data["reasoning"] == {"effort": "low"}
    assert options["instructions"] == data["instructions"] == config.instructions
    assert options["model"] == data["model"] == settings.model_deployment
    assert options["tools"] == [{
        "type": "azure_ai_search",
        "azure_ai_search": {"indexes": [{
            "project_connection_id": settings.search_connection_id,
            "index_name": "documents", "query_type": "simple", "top_k": 5,
        }]},
    }]
    assert options["store"] is False
    assert options["include"] == ["reasoning.encrypted_content"]


async def test_real_sdk_serialization_and_dedicated_routes(settings, raw_response):
    requests = []

    def transport(request):
        requests.append(request)
        return httpx2.Response(200, json=raw_response)

    async with AIProjectClient(endpoint=settings.project_endpoint, credential=Credential()) as project:
        for name in (None, settings.prompt_agent, settings.hosted_agent):
            async with project.get_openai_client(
                agent_name=name, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)),
            ) as client:
                options = model_options(settings, load_config()) if name is None else {
                    "max_output_tokens": 4096, "include": ["reasoning.encrypted_content"], "store": False,
                }
                response = await client.responses.create(input="Question", **options)
                # The real installed OpenAI SDK retains unknown native extension fields.
                output = response.model_dump(mode="json", exclude_unset=True, warnings=False)["output"]
                assert output[1]["type"] == "azure_ai_search_call_output"
                assert output[1]["output"]["results"][0]["id"] == "doc-1"
        assert str(requests[0].url).split("?")[0].endswith("/openai/v1/responses")
        assert f"/agents/{settings.prompt_agent}/endpoint/protocols/openai/responses" in str(requests[1].url)
        assert f"/agents/{settings.hosted_agent}/endpoint/protocols/openai/responses" in str(requests[2].url)
        direct = json.loads(requests[0].content)
        assert direct["tools"] == model_options(settings, load_config())["tools"]
        assert "agent_reference" not in direct


@pytest.mark.parametrize("field,value", [
    ("project_endpoint", "https://attacker.example/api/projects/test"),
    ("project_endpoint", "https://example.services.ai.azure.com/api/projects/p?override=1"),
    ("prompt_agent", "../other"),
    ("search_connection_id", "https://search.example/index"),
])
def test_settings_reject_arbitrary_destinations(settings, field, value):
    with pytest.raises(ValueError):
        type(settings).model_validate({**settings.model_dump(), field: value})
