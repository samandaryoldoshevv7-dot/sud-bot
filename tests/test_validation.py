import json

import pytest
from pydantic import ValidationError

from app.ai.base import AIResponseError, LLMProvider
from app.ai.structured import extract_json, structured_call
from app.schemas.ai import GeneratedQuestion, GenerationResponse
from app.services.question_validation import check_question, find_excerpt, is_duplicate

SOURCE = "Apellyatsiya shikoyati hal qiluv qarori e'lon qilingan kundan e'tiboran o'n kun ichida beriladi."
GOOD = dict(
    question="Apellyatsiya shikoyati qancha muddatda beriladi?",
    options={"A": "Besh kun", "B": "O'n kun", "C": "Bir oy", "D": "Uch kun"},
    correct="B",
    explanation="Manbaga ko'ra o'n kun ichida beriladi.",
    excerpt="hal qiluv qarori e’lon qilingan kundan e’tiboran o‘n kun ichida beriladi",
    expected_options=4,
    sources=[(7, SOURCE)],
    min_excerpt_score=88,
)


def test_valid_question_passes_and_locates_excerpt():
    result = check_question(**GOOD)
    assert result.ok, result.errors
    assert result.match is not None and result.match.chunk_id == 7


@pytest.mark.parametrize(
    "change,error",
    [
        ({"options": {"A": "Besh kun", "B": "O'n kun", "C": "o'n  kun", "D": "Uch kun"}}, "duplicate_options"),
        (
            {"options": {"A": "Besh kun", "B": "O'n kun", "C": "Barchasi to'g'ri", "D": "Uch kun"}},
            "forbidden_option_pattern",
        ),
        ({"correct": "E"}, "correct_not_in_options"),
        (
            {"excerpt": "Kassatsiya shikoyati bir yil ichida beriladi va bu manbada yo'q."},
            "excerpt_not_found_in_source",
        ),
        ({"options": {"A": "Besh kun", "B": "O'n kun", "C": "Bir oy"}}, "option_count_3_expected_4"),
        ({"explanation": ""}, "explanation_missing"),
        ({"options": {"A": "Besh kun", "B": "", "C": "Bir oy", "D": "Uch kun"}}, "empty_option"),
    ],
)
def test_invalid_questions_are_rejected(change, error):
    result = check_question(**{**GOOD, **change})
    assert error in result.errors


def test_fuzzy_excerpt_threshold():
    assert find_excerpt("o'n kun ichida beriladi hal qiluv", [(1, SOURCE)], 95) is None
    assert find_excerpt("Apellyatsiya shikoyati hal qiluv qarori", [(1, "boshqa matn"), (2, SOURCE)], 88).chunk_id == 2


def test_duplicate_detection():
    assert is_duplicate(
        "Apellyatsiya shikoyati qancha muddatda beriladi?", ["apellyatsiya shikoyati qancha muddatda beriladi"]
    )
    assert not is_duplicate("Arxivda ishlar necha yil saqlanadi?", ["Apellyatsiya shikoyati qancha muddatda beriladi?"])


def test_pydantic_question_schema():
    data = {
        "question": "Apellyatsiya shikoyati qancha muddatda beriladi?",
        "options": {"a": "Besh kun", "B)": "O'n kun", "C": "Bir oy", "D": "Uch kun"},
        "correct_answer": "b",
        "explanation": "Manbaga ko'ra o'n kun.",
        "topic": "Muddatlar",
        "difficulty": "Medium",
        "source_chunk_id": 3,
        "source_excerpt": SOURCE,
    }
    q = GeneratedQuestion.model_validate(data)
    assert q.correct_answer == "B" and list(q.options) == ["A", "B", "C", "D"] and q.difficulty == "medium"
    with pytest.raises(ValidationError):
        GeneratedQuestion.model_validate({**data, "correct_answer": "E"})
    with pytest.raises(ValidationError):
        GeneratedQuestion.model_validate({**data, "options": {"A": "x", "C": "y", "D": "z"}})
    resp = GenerationResponse.model_validate({"status": "REJECTED", "questions": None, "rejection_reason": "kam"})
    assert resp.status == "rejected" and resp.questions == []


def test_extract_json_tolerates_fences():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go: {"a": 2} thanks') == {"a": 2}


class _Flaky(LLMProvider):
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.seen_messages = []

    @property
    def default_model(self):
        return "flaky"

    async def complete_json(self, messages, **kw):
        self.seen_messages.append(messages)
        return self.outputs.pop(0)


async def test_structured_call_retries_on_invalid_json():
    good = json.dumps({"supported": True, "explanation": "Manbaga ko'ra."})
    from app.schemas.ai import ExplanationResponse

    provider = _Flaky(["not json at all", '{"supported": "maybe"}', good])
    result = await structured_call(provider, "sys", "user", ExplanationResponse, retries=3)
    assert result.supported is True
    # Retry messages include the validation error feedback.
    assert "invalid" in provider.seen_messages[1][-1]["content"].lower()


async def test_structured_call_gives_up():
    from app.schemas.ai import ExplanationResponse

    provider = _Flaky(["nope", "nope", "nope"])
    with pytest.raises(AIResponseError):
        await structured_call(provider, "sys", "user", ExplanationResponse, retries=3)
