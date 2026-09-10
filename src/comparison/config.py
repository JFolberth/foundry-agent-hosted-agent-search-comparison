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
    prompt_agent_none: str | None = None
    hosted_agent_none: str | None = None

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
        agent_names = [self.prompt_agent, self.hosted_agent, self.prompt_agent_none, self.hosted_agent_none]
        configured_agent_names = [name for name in agent_names if name is not None]
        for name in (self.model_deployment, self.search_index, *configured_agent_names):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
                raise ValueError("Invalid configured resource name")
        if len(set(configured_agent_names)) != len(configured_agent_names):
            raise ValueError("All configured registered agent names must be distinct")
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
        values = {key: os.environ[value] for key, value in names.items()}
        # Optional: only the web container registers the additional no-reasoning
        # agents, so the hosted container's own environment need not supply them.
        optional_names = {
            "prompt_agent_none": "PROMPT_AGENT_NAME_NONE",
            "hosted_agent_none": "HOSTED_AGENT_NAME_NONE",
        }
        for key, env_name in optional_names.items():
            if env_name in os.environ:
                values[key] = os.environ[env_name]
        return cls(**values)


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


def resolved_reasoning_effort(config: AgentConfig) -> str:
    """The hosted container's own model call effort. Defaults to the canonical
    agent.json value, but Terraform can register a second hosted agent that
    reuses the SAME image with REASONING_EFFORT_OVERRIDE=none, so one image
    serves both the low- and no-reasoning hosted registrations."""
    override = os.environ.get("REASONING_EFFORT_OVERRIDE")
    if override is None:
        return config.reasoning_effort
    if override not in ("low", "none"):
        raise ValueError("REASONING_EFFORT_OVERRIDE must be 'low' or 'none'")
    return override


def model_options(settings: Settings, config: AgentConfig) -> dict:
    return {
        "model": settings.model_deployment,
        "instructions": config.instructions,
        "tools": [native_search_tool(settings, config).as_dict()],
        "reasoning": {"effort": resolved_reasoning_effort(config)},
        "max_output_tokens": config.max_output_tokens,
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }
