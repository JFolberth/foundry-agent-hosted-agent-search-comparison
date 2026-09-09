import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from azure.ai.projects.models import (
    AISearchIndexResource,
    AzureAISearchTool,
    AzureAISearchToolResource,
    PromptAgentDefinition,
    Reasoning,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[2]


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    instructions: str = Field(min_length=1, max_length=16000)
    reasoning_effort: Literal["low"]
    search_query_type: Literal["simple"]
    search_top_k: Literal[5]
    max_output_tokens: Literal[4096]


def load_config() -> AgentConfig:
    return AgentConfig.model_validate_json((ROOT / "config/agent.json").read_text())


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_endpoint: str
    model_deployment: str
    search_connection_id: str
    search_index: str
    prompt_agent: str
    hosted_agent: str

    @model_validator(mode="after")
    def validate_fixed_endpoints(self):
        endpoint = urlsplit(self.project_endpoint)
        if (
            endpoint.scheme != "https"
            or not (endpoint.hostname or "").endswith(".services.ai.azure.com")
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
            or endpoint.port not in (None, 443)
            or not re.fullmatch(r"/api/projects/[\w-]+/?", endpoint.path)
        ):
            raise ValueError("A fixed Azure Foundry project endpoint is required")
        for name in (self.model_deployment, self.search_index, self.prompt_agent, self.hosted_agent):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
                raise ValueError("Invalid configured resource name")
        if self.prompt_agent == self.hosted_agent:
            raise ValueError("Two distinct registered agent names are required")
        if not re.fullmatch(
            r"/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.CognitiveServices/"
            r"accounts/[^/]+/projects/[^/]+/connections/[^/]+",
            self.search_connection_id, re.IGNORECASE,
        ):
            raise ValueError("Search connection must be a Foundry project connection ARM ID")
        return self

    @classmethod
    def from_env(cls):
        names = {
            "project_endpoint": "FOUNDRY_PROJECT_ENDPOINT",
            "model_deployment": "MODEL_DEPLOYMENT_NAME",
            "search_connection_id": "SEARCH_PROJECT_CONNECTION_ID",
            "search_index": "SEARCH_INDEX_NAME",
            "prompt_agent": "PROMPT_AGENT_NAME",
            "hosted_agent": "HOSTED_AGENT_NAME",
        }
        return cls(**{key: os.environ[value] for key, value in names.items()})


def native_search_tool(settings: Settings, config: AgentConfig) -> AzureAISearchTool:
    return AzureAISearchTool(
        azure_ai_search=AzureAISearchToolResource(indexes=[
            AISearchIndexResource(
                project_connection_id=settings.search_connection_id,
                index_name=settings.search_index,
                query_type=config.search_query_type,
                top_k=config.search_top_k,
            )
        ])
    )


def prompt_definition(settings: Settings, config: AgentConfig) -> PromptAgentDefinition:
    """Schema parity reference; deployment owns registration, never this runtime."""
    return PromptAgentDefinition(
        model=settings.model_deployment,
        instructions=config.instructions,
        reasoning=Reasoning(effort=config.reasoning_effort),
        tools=[native_search_tool(settings, config)],
    )


def model_options(settings: Settings, config: AgentConfig) -> dict:
    return {
        "model": settings.model_deployment,
        "instructions": config.instructions,
        "tools": [native_search_tool(settings, config).as_dict()],
        "reasoning": {"effort": config.reasoning_effort},
        "max_output_tokens": config.max_output_tokens,
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }
