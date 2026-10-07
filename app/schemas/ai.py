"""Pydantic schemas for every structured AI response. Invalid output never reaches the DB."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LETTERS = ("A", "B", "C", "D", "E")
Letter = Literal["A", "B", "C", "D", "E"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class GeneratedQuestion(_Strict):
    question: str = Field(min_length=10, max_length=700)
    options: dict[str, str]
    correct_answer: Letter
    explanation: str = Field(min_length=10, max_length=2000)
    topic: str = Field(min_length=2, max_length=120)
    difficulty: Literal["easy", "medium", "hard"]
    source_chunk_id: int
    source_reference: str = Field(default="", max_length=500)
    source_excerpt: str = Field(min_length=15, max_length=2000)

    @field_validator("correct_answer", mode="before")
    @classmethod
    def _upper_letter(cls, value: object) -> object:
        return value.strip().upper()[:1] if isinstance(value, str) else value

    @field_validator("difficulty", mode="before")
    @classmethod
    def _lower_difficulty(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("options", mode="before")
    @classmethod
    def _normalize_options(cls, value: object) -> object:
        if isinstance(value, list):  # tolerate ["...", "..."] lists
            return {LETTERS[i]: str(v) for i, v in enumerate(value[: len(LETTERS)])}
        if isinstance(value, dict):
            return {str(k).strip().upper().rstrip(").:"): str(v).strip() for k, v in value.items()}
        return value

    @model_validator(mode="after")
    def _check_options(self) -> GeneratedQuestion:
        keys = list(self.options.keys())
        if not 3 <= len(keys) <= 5:
            raise ValueError("options must contain between 3 and 5 entries")
        if keys != list(LETTERS[: len(keys)]):
            raise ValueError(f"option keys must be consecutive letters starting at A, got {keys}")
        if any(not v for v in self.options.values()):
            raise ValueError("options must not be empty")
        if self.correct_answer not in self.options:
            raise ValueError("correct_answer must be one of the option keys")
        return self


class GenerationResponse(_Strict):
    status: Literal["ok", "rejected"]
    questions: list[GeneratedQuestion] = Field(default_factory=list)
    rejection_reason: str | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _lower(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("questions", mode="before")
    @classmethod
    def _drop_invalid(cls, value: object) -> object:
        # Each question is validated individually later so one bad item does not void the batch.
        return value if isinstance(value, list) else []


class SourceVerification(_Strict):
    """Blind answer: the verifier answers the question from the source without seeing the key."""

    answer: Letter | None = None
    supported: bool
    evidence: str = ""
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("answer", mode="before")
    @classmethod
    def _letter(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().upper()[:1]
            return value or None
        return value


class QualityValidation(_Strict):
    understandable: bool
    single_correct: bool
    options_meaningful: bool
    ambiguous: bool
    explanation_consistent: bool
    issues: list[str] = Field(default_factory=list)
    verdict: Literal["accept", "reject"]

    @field_validator("verdict", mode="before")
    @classmethod
    def _lower(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class TopicAssignment(_Strict):
    index: int
    topic: str = Field(min_length=2, max_length=120)


class TopicClassification(_Strict):
    topics: list[TopicAssignment]


class ExplanationResponse(_Strict):
    supported: bool
    explanation: str = ""
