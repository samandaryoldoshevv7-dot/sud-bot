"""Factories wiring the configured AI provider into the question generator."""

from __future__ import annotations

from app.ai.base import AINotConfiguredError
from app.ai.factory import get_llm_provider
from app.config import get_settings
from app.services.question_generation import QuestionGenerator


def ai_available() -> bool:
    return get_settings().ai_enabled


def make_generator() -> QuestionGenerator:
    if not ai_available():
        raise AINotConfiguredError("GROQ_API_KEY is not configured")
    return QuestionGenerator(get_llm_provider())
