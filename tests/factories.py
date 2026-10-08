"""Helpers that create real rows through the application services (no fake statistics)."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import timedelta

from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.base import LLMProvider
from app.models import (
    Difficulty,
    FileType,
    Question,
    QuestionStatus,
    SourceKind,
    TestDifficulty,
    User,
    UserStatus,
)
from app.rag.embeddings import EmbeddingProvider
from app.services import users as user_service
from app.services.test_builder import TestDraftData, create_test
from app.utils.text import text_hash
from app.utils.time import utcnow

LAW_TEXT = """SUD ISHLARINI YURITISH TO'G'RISIDA YO'RIQNOMA

1-modda. Umumiy qoidalar
Sudga kelib tushgan har bir ariza kelib tushgan kunning o'zida ro'yxatga olinadi. Ro'yxatga olish elektron jurnalda amalga oshiriladi va arizaga tartib raqami beriladi.

2-modda. Apellyatsiya muddati
Apellyatsiya shikoyati hal qiluv qarori e'lon qilingan kundan e'tiboran o'n kun ichida beriladi. Muddat o'tkazib yuborilgan taqdirda uni tiklash to'g'risida iltimosnoma berilishi mumkin.

3-modda. Sud majlisi bayonnomasi
Sud majlisining bayonnomasi majlis tugaganidan keyin uch kun ichida rasmiylashtiriladi va raislik qiluvchi hamda kotib tomonidan imzolanadi.

4-modda. Arxivga topshirish
Ko'rib chiqilgan ishlar sud qarori qonuniy kuchga kirganidan so'ng bir oy ichida arxivga topshiriladi. Arxivda ishlar kamida besh yil saqlanadi.

5-modda. Sud odobi
Sud xodimi fuqarolarga nisbatan xushmuomala bo'lishi, ularning murojaatlarini o'z vaqtida ko'rib chiqishi shart. Xodim xizmat vazifasini bajarishda xolis bo'lishi lozim.
"""


def tg_user(user_id: int, first: str = "Ali", last: str | None = "Valiyev", username: str | None = None) -> TgUser:
    return TgUser(id=user_id, is_bot=False, first_name=first, last_name=last, username=username)


async def make_employee(session: AsyncSession, tg_id: int, name: str = "Valiyev Ali", active: bool = True) -> User:
    user = await user_service.upsert_user(session, tg_user(tg_id, name.split()[0], name.split()[-1]), private_chat=True)
    await user_service.set_full_name(session, user, name)
    if active:
        await user_service.set_status(session, user.id, UserStatus.ACTIVE)
        await session.refresh(user)
    return user


async def make_question(
    session: AsyncSession,
    *,
    text_: str,
    correct: str = "B",
    kind: SourceKind = SourceKind.MATERIAL,
    material_id: int | None = None,
    news_id: int | None = None,
    status: QuestionStatus = QuestionStatus.APPROVED,
    difficulty: Difficulty = Difficulty.MEDIUM,
    topic_id: int | None = None,
    options: dict[str, str] | None = None,
) -> Question:
    q = Question(
        question_text=text_,
        options=options
        or {"A": f"{text_} variant A", "B": f"{text_} variant B", "C": f"{text_} variant C", "D": f"{text_} variant D"},
        option_count=len(options or "ABCD"),
        correct_option=correct,
        explanation="Manbada shunday ko'rsatilgan.",
        topic_id=topic_id,
        difficulty=difficulty,
        status=status,
        source_kind=kind,
        source_material_id=material_id,
        source_news_id=news_id,
        source_reference="Yo'riqnoma, 1-bet",
        source_excerpt="Sudga kelib tushgan har bir ariza kelib tushgan kunning o'zida ro'yxatga olinadi.",
        text_hash=text_hash(text_),
    )
    session.add(q)
    await session.commit()
    return q


async def make_material_with_text(
    session_maker,
    text: str = LAW_TEXT,
    embeddings: EmbeddingProvider | None = None,
    title: str = "Yo'riqnoma",
    file_name: str | None = None,
):
    from app.services import materials as material_service

    async with session_maker() as session:
        material = await material_service.create_material(
            session, title=title, file_type=FileType.TEXT, uploaded_by_id=None, raw_text=text, file_name=file_name
        )
    result = await material_service.process_material(session_maker, material.id, None, embeddings or HashEmbeddings())
    assert result.ok, result
    return material.id


def draft(question_count: int = 4, **kw) -> TestDraftData:
    now = utcnow()
    data = dict(
        title="Sud amaliyoti — 2026",
        question_count=question_count,
        starts_at=now - timedelta(minutes=1),
        deadline_at=now + timedelta(hours=24),
        option_count=4,
        difficulty=TestDifficulty.MIXED,
        use_all_materials=True,
        randomize_questions=True,
        randomize_options=True,
        passing_percent=60,
    )
    data.update(kw)
    return TestDraftData(**data)


async def make_test(session: AsyncSession, admin_id: int | None = None, **kw):
    return await create_test(session, draft(**kw), admin_id)


class HashEmbeddings(EmbeddingProvider):
    """Deterministic bag-of-words embedding for tests (real vectors, no network)."""

    name = "test-hash"
    model_name = "test-hash"
    dim = 384

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for t in texts:
            vec = [0.0] * self.dim
            for word in re.findall(r"\w+", t.lower()):
                h = int(hashlib.md5(word.encode()).hexdigest(), 16)
                vec[h % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors


class ScriptedLLM(LLMProvider):
    """Test double for the LLM: answers each prompt type from the real source text it receives.

    Generation copies a real sentence from the primary chunk as the excerpt, so the full
    validation pipeline (excerpt matching, verification, topics) runs for real.
    """

    name = "scripted"

    def __init__(
        self,
        verifier_agrees: bool = True,
        invalid_first: bool = False,
        bad_excerpt: bool = False,
        wrong_source: bool = False,
    ):
        self.wrong_source = wrong_source
        self.verifier_agrees = verifier_agrees
        self.invalid_first = invalid_first
        self.bad_excerpt = bad_excerpt
        self.calls: list[str] = []
        self._counter = 0
        self._answers: dict[str, str] = {}

    @property
    def default_model(self) -> str:
        return "scripted-model"

    async def complete_json(self, messages, *, model=None, temperature=None, max_tokens=2048) -> str:
        system = messages[0]["content"]
        user = messages[1]["content"]
        if "exam author" in system:
            self.calls.append("generation")
            if self.invalid_first and self.calls.count("generation") == 1 and len(messages) == 2:
                return "this is not json"
            return self._generate(user)
        if "independent fact checker" in system:
            self.calls.append("verification")
            question = user.split("QUESTION:\n", 1)[1].split("\n\nOPTIONS:", 1)[0].strip()
            answer = self._answers.get(question, "A")
            if not self.verifier_agrees:
                answer = "D" if answer != "D" else "C"
            return json.dumps({"answer": answer, "supported": True, "evidence": "x", "confidence": 0.95})
        if "quality reviewer" in system:
            self.calls.append("quality")
            return json.dumps(
                {
                    "understandable": True,
                    "single_correct": True,
                    "options_meaningful": True,
                    "ambiguous": False,
                    "explanation_consistent": True,
                    "issues": [],
                    "verdict": "accept",
                }
            )
        if "classify exam questions" in system:
            self.calls.append("topics")
            payload = json.loads(user.split("QUESTIONS:\n", 1)[1])
            return json.dumps({"topics": [{"index": p["index"], "topic": "Protsessual muddatlar"} for p in payload]})
        if "short explanations" in system:
            self.calls.append("explanation")
            return json.dumps({"supported": True, "explanation": "Manbaga ko'ra shunday."})
        raise AssertionError("unexpected prompt")

    def _generate(self, user: str) -> str:
        count = int(re.search(r"Write (\d+) multiple-choice", user).group(1))
        n_opts = int(re.search(r"exactly (\d) options", user).group(1))
        chunk_id, attrs, chunk_text = re.search(r'<chunk id="(\d+)"([^>]*)>\n(.*?)\n</chunk>', user, re.S).groups()
        source = re.search(r'source="([^"]*)"', attrs).group(1)
        source_file = re.search(r'source_file="([^"]*)"', attrs).group(1)
        if self.wrong_source:
            source, source_file = "Boshqa hujjat", "boshqa.pdf"
        sentences = [
            s.strip() for s in re.split(r"(?<=[.!?])\s+", chunk_text.replace("\n", " ")) if len(s.strip()) > 30
        ]
        questions = []
        for _i in range(count):
            self._counter += 1
            sentence = sentences[(self._counter - 1) % len(sentences)] if sentences else chunk_text[:120]
            excerpt = "Bu jumla manbada umuman yo'q va to'qib chiqarilgan." if self.bad_excerpt else sentence
            correct = "ABCDE"[self._counter % n_opts]
            qtext = f"Manbaga ko'ra quyidagi qoida qanday to'g'ri davom etadi: «{sentence[:45]}»?"
            options = {"ABCDE"[k]: f"Variant {self._counter}-{k} matni" for k in range(n_opts)}
            options[correct] = f"To'g'ri javob {self._counter}: {sentence[:60]}"
            self._answers[qtext] = correct
            questions.append(
                {
                    "question": qtext,
                    "options": options,
                    "correct_answer": correct,
                    "explanation": "Manbada aynan shunday deyilgan.",
                    "topic": "Muddatlar",
                    "difficulty": "medium",
                    "source_chunk_id": int(chunk_id),
                    "source_reference": "ref",
                    "source_excerpt": excerpt,
                    "source": source,
                    "source_file": source_file,
                }
            )
        return json.dumps({"status": "ok", "questions": questions, "rejection_reason": None}, ensure_ascii=False)
