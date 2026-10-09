from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatRequest(StrictModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: UUID | None = None

    @field_validator("message")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("message is blank")
        return value


class InternalChat(ChatRequest):
    user_id: StrictInt = Field(gt=0)


class Claim(StrictModel):
    text: str = Field(min_length=1, max_length=1500)
    evidence_ids: list[str] = Field(min_length=1, max_length=10)


class ResponseClaim(StrictModel):
    text: str = Field(min_length=1, max_length=1500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)


class ModelAnswer(StrictModel):
    conclusion: Claim
    suggestions: list[Claim] = Field(max_length=5)
    limitations: list[str] = Field(max_length=8)


class Suggestion(Claim):
    equipment_operation: bool = False


class Evidence(StrictModel):
    id: str
    tool: str
    status: str
    source: str
    collected_at: str
    data_time: str | None = None
    data: dict | list
    kind: Literal["business", "document", "web"] = "business"
    document_version: str | None = None
    chapter: str | None = None
    url: str | None = None


class ChatResponse(StrictModel):
    request_id: str
    session_id: str
    status: Literal["answered", "unable_to_determine", "degraded"]
    conclusion: ResponseClaim | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    model: str
    knowledge_status: Literal["not_requested", "hit", "miss", "index_unavailable"] = "not_requested"
    web_status: str = "not_requested"
    notices: list[str] = Field(default_factory=list)
    miss_record_status: str = "not_requested"
    answer_mode: Literal["grounded", "hypothesis"] = "grounded"
    source_coverage_percent: int = Field(default=0, ge=0, le=100)
    sourced_claims: int = 0
    total_claims: int = 0
