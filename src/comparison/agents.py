"""Canonical registry of comparison sides: which Foundry agent each one calls,
its underlying orchestration kind (server-orchestrated prompt vs. self-hosted
container), and its configured reasoning effort. Single source of truth so the
backend derives behavior from structured metadata instead of parsing side
name strings in multiple places.
"""

from typing import Literal, TypedDict

Kind = Literal["prompt", "hosted"]
ReasoningEffort = Literal["low", "none"]


class AgentMeta(TypedDict):
    kind: Kind
    reasoning_effort: ReasoningEffort


AGENT_SIDES: dict[str, AgentMeta] = {
    "prompt": {"kind": "prompt", "reasoning_effort": "low"},
    "hosted": {"kind": "hosted", "reasoning_effort": "low"},
    "prompt_none": {"kind": "prompt", "reasoning_effort": "none"},
    "hosted_none": {"kind": "hosted", "reasoning_effort": "none"},
}


def kind_of(side: str) -> Kind:
    return AGENT_SIDES[side]["kind"]


def reasoning_of(side: str) -> ReasoningEffort:
    return AGENT_SIDES[side]["reasoning_effort"]
