from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_MESSAGE = 4000
MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_CHARS = 24000
MAX_RAW_BYTES = 524288
MAX_RAW_ITEMS = 160
MAX_TURNS = 10
REQUEST_TIMEOUT = 90
MODEL_TIMEOUT = 75


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Message(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12000)


class History(StrictModel):
    prompt: list[Message] = Field(default_factory=list, max_length=MAX_HISTORY_MESSAGES)
    hosted: list[Message] = Field(default_factory=list, max_length=MAX_HISTORY_MESSAGES)

    @model_validator(mode="after")
    def total_content(self):
        for messages in (self.prompt, self.hosted):
            if sum(len(m.content) for m in messages) > MAX_HISTORY_CHARS:
                raise ValueError("History exceeds character limit")
        return self


class Continuation(StrictModel):
    prompt: str | None = Field(default=None, min_length=40, max_length=64, pattern=r"^[\w-]+$")
    hosted: str | None = Field(default=None, min_length=40, max_length=64, pattern=r"^[\w-]+$")


class CompareRequest(StrictModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE)
    history: History = Field(default_factory=History)
    continuation: Continuation = Field(default_factory=Continuation)

    @field_validator("message")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Message must not be blank")
        return value
