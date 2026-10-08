"""Source-grounded question generation (RAG + LLM + multi-stage validation).

Pipeline for each seed chunk:
  retrieve context → LLM generation (JSON) → Pydantic validation → deterministic checks
  (option sanity, excerpt located verbatim/fuzzily in the source chunk, duplicates)
  → AI source verification (blind answer must match the key) → AI quality validation
  → (explanation regeneration if inconsistent) → topic classification → stored as PENDING.

Anything failing a step is rejected and logged; nothing invalid is stored silently.
"""

from __future__ import annotations

import logging
import random
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import prompts
from app.ai.base import AIError, AIResponseError, LLMProvider
from app.ai.structured import structured_call
from app.config import get_settings
from app.models import (
    Difficulty,
    Material,
    News,
    Question,
    QuestionOrigin,
    QuestionStatus,
    SourceChunk,
    SourceKind,
    TestDifficulty,
    Topic,
)
from app.rag.retriever import Retriever, chunk_reference
from app.rag.vector_store import SourceScope
from app.schemas.ai import (
    ExplanationResponse,
    GeneratedQuestion,
    GenerationResponse,
    QualityValidation,
    QualityValidationBatch,
    SourceVerification,
    SourceVerificationBatch,
)
from app.services.question_validation import check_question, is_duplicate
from app.utils.text import normalize_topic, text_hash

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], Awaitable[None]]

MIXED_PLAN = ["medium", "easy", "medium", "hard", "medium", "easy", "hard", "medium", "easy", "medium"]


@dataclass
class GenerationRequest:
    scope: SourceScope
    count: int
    option_count: int = 4
    difficulty: TestDifficulty = TestDifficulty.MIXED
    focus_query: str | None = None
    exclude_chunk_ids: set[int] = field(default_factory=set)
    max_llm_rounds: int | None = None


@dataclass
class GenerationResult:
    questions: list[Question] = field(default_factory=list)
    rejected: Counter = field(default_factory=Counter)
    rounds: int = 0
    seeds_available: int = 0
    error: str | None = None

    @property
    def created(self) -> int:
        return len(self.questions)


@dataclass
class _Candidate:
    data: GeneratedQuestion
    chunk: SourceChunk
    reference: str
    kind: SourceKind
    verification: dict


def plan_difficulties(difficulty: TestDifficulty, n: int, offset: int = 0) -> list[str]:
    if difficulty != TestDifficulty.MIXED:
        return [difficulty.value] * n
    return [MIXED_PLAN[(offset + i) % len(MIXED_PLAN)] for i in range(n)]


class QuestionGenerator:
    def __init__(self, llm: LLMProvider, retriever: Retriever | None = None, rng: random.Random | None = None):
        self.llm = llm
        self.retriever = retriever or Retriever()
        self.rng = rng or random.Random()
        self.settings = get_settings()

    # ------------------------------------------------------------------ public API

    async def generate(
        self,
        session: AsyncSession,
        request: GenerationRequest,
        progress: ProgressCallback | None = None,
    ) -> GenerationResult:
        result = GenerationResult()
        if request.count <= 0 or request.scope.empty:
            return result
        per_chunk = max(1, self.settings.ai_questions_per_chunk)
        max_rounds = request.max_llm_rounds or max(4, request.count * 2)
        seeds = await self.retriever.seed_chunks(
            session,
            request.scope,
            count=min(max_rounds, request.count * 2 + 2),
            focus_query=request.focus_query,
            exclude_ids=request.exclude_chunk_ids,
        )
        result.seeds_available = len(seeds)
        if not seeds:
            result.error = "no_source_chunks"
            return result

        titles = await self._source_titles(session, seeds)
        planned_offset = 0
        seed_index = 0
        created_this_pass = 0
        while result.created < request.count and result.rounds < max_rounds:
            if seed_index >= len(seeds):
                # Small sources have few chunks: make another pass over the same chunks (the prompt
                # lists already generated questions so the model must find different facts), but
                # stop as soon as a whole pass yields nothing new.
                if created_this_pass == 0:
                    break
                seed_index = 0
                created_this_pass = 0
            seed = seeds[seed_index]
            seed_index += 1
            result.rounds += 1
            remaining = request.count - result.created
            n = min(per_chunk, remaining)
            difficulties = plan_difficulties(request.difficulty, n, planned_offset)
            planned_offset += n
            try:
                created = await self._generate_from_seed(session, seed, request, n, difficulties, titles, result)
            except AIError as exc:
                logger.error("AI failure during generation", extra={"chunk_id": seed.id, "error": str(exc)[:300]})
                result.rejected["ai_error"] += 1
                result.error = str(exc)[:300]
                if any(m in str(exc).lower() for m in ("authentication", "unavailable", "rate limit")):
                    break  # retrying other chunks cannot help (a rate limit would only make it wait longer)
                continue
            seed.generation_attempts += 1
            await session.commit()
            result.questions.extend(created)
            created_this_pass += len(created)
            if progress:
                await progress(result.created, request.count)
        logger.info(
            "Question generation finished",
            extra={
                "created_count": result.created,
                "requested": request.count,
                "rounds": result.rounds,
                "rejected": dict(result.rejected),
            },
        )
        return result

    async def regenerate_explanation(
        self, source_text: str, question: str, options: dict[str, str], correct: str
    ) -> str | None:
        response = await structured_call(
            self.llm,
            prompts.EXPLANATION_SYSTEM,
            prompts.explanation_user(source_text, question, options, correct),
            ExplanationResponse,
            retries=self.settings.ai_max_retries,
            temperature=0.1,
            max_tokens=600,
            purpose="explanation",
        )
        if not response.supported or len(response.explanation.strip()) < 10:
            return None
        return response.explanation.strip()

    async def verify_existing(
        self, source_text: str, question: str, options: dict[str, str], correct: str
    ) -> tuple[bool, dict]:
        """Re-run AI source verification for an admin-edited question."""
        verification = await self._source_verification(source_text, question, options)
        ok = verification.supported and verification.answer == correct
        return ok, verification.model_dump()

    # ------------------------------------------------------------------ internals

    async def _source_titles(self, session: AsyncSession, chunks: list[SourceChunk]) -> dict[tuple[str, int], str]:
        material_ids = {c.material_id for c in chunks if c.material_id}
        news_ids = {c.news_id for c in chunks if c.news_id}
        titles: dict[tuple[str, int], str] = {}
        if material_ids:
            for mid, title, file_name in await session.execute(
                select(Material.id, Material.title, Material.file_name).where(Material.id.in_(material_ids))
            ):
                titles[("material", mid)] = title
                titles[("material_file", mid)] = file_name or title
        if news_ids:
            for nid, title in await session.execute(select(News.id, News.title).where(News.id.in_(news_ids))):
                titles[("news", nid)] = title
        return titles

    def _ref(self, chunk: SourceChunk, titles: dict[tuple[str, int], str]) -> tuple[str, SourceKind]:
        if chunk.material_id is not None:
            return chunk_reference(
                chunk, titles.get(("material", chunk.material_id), "Material"), "material"
            ), SourceKind.MATERIAL
        return chunk_reference(chunk, titles.get(("news", chunk.news_id or 0), "Yangilik"), "news"), SourceKind.NEWS

    def _identity(self, chunk: SourceChunk, titles: dict[tuple[str, int], str]) -> tuple[str, str]:
        """The real document of a chunk: (source name, original file name)."""
        if chunk.material_id is not None:
            name = titles.get(("material", chunk.material_id), "")
            return name, titles.get(("material_file", chunk.material_id), name)
        name = titles.get(("news", chunk.news_id or 0), "")
        return name, name

    async def _existing_questions(self, session: AsyncSession, chunk_ids: list[int]) -> list[str]:
        stmt = select(Question.question_text).where(Question.source_chunk_id.in_(chunk_ids)).limit(50)
        return list((await session.execute(stmt)).scalars().all())

    async def _generate_from_seed(
        self,
        session: AsyncSession,
        seed: SourceChunk,
        request: GenerationRequest,
        n: int,
        difficulties: list[str],
        titles: dict[tuple[str, int], str],
        result: GenerationResult,
    ) -> list[Question]:
        context = await self.retriever.context_for(session, seed, request.scope)
        missing = [
            c for c in context if (("material", c.material_id) if c.material_id else ("news", c.news_id)) not in titles
        ]
        if missing:
            titles.update(await self._source_titles(session, missing))
        by_id = {c.id: c for c in context}
        ctx = [prompts.ContextChunk(c.id, self._ref(c, titles)[0], c.text, *self._identity(c, titles)) for c in context]
        existing = await self._existing_questions(session, list(by_id))
        topic_names = await self._topic_names(session)

        response = await structured_call(
            self.llm,
            prompts.QUESTION_GENERATION_SYSTEM,
            prompts.question_generation_user(
                ctx,
                count=n,
                option_count=request.option_count,
                difficulties=difficulties,
                focus=request.focus_query,
                avoid_questions=existing,
                existing_topics=topic_names,
            ),
            GenerationResponse,
            retries=self.settings.ai_max_retries,
            max_tokens=900 * n + 400,
            purpose="generation",
        )
        if response.status == "rejected" or not response.questions:
            result.rejected["model_rejected_source"] += 1
            logger.info(
                "Model rejected source chunk",
                extra={"chunk_id": seed.id, "reason": (response.rejection_reason or "")[:200]},
            )
            return []

        pending: list[tuple[GeneratedQuestion, SourceChunk, float]] = []
        seen = list(existing)
        sources = [(c.id, c.text) for c in context]
        for item in response.questions[:n]:
            check = check_question(
                question=item.question,
                options=item.options,
                correct=item.correct_answer,
                explanation=item.explanation,
                excerpt=item.source_excerpt,
                expected_options=request.option_count,
                sources=sources,
                min_excerpt_score=self.settings.ai_excerpt_min_similarity,
                claimed_chunk_id=item.source_chunk_id,
            )
            if not check.ok:
                for err in check.errors:
                    result.rejected[err] += 1
                logger.info("Generated question failed checks", extra={"errors": ",".join(check.errors)})
                continue
            if is_duplicate(item.question, seen) or await self._hash_exists(session, item.question):
                result.rejected["duplicate"] += 1
                continue
            chunk = by_id[check.match.chunk_id]  # type: ignore[union-attr]
            # The source must be named and must be the document the excerpt really comes from.
            if not item.source.strip() or not item.source_file.strip():
                result.rejected["missing_source"] += 1
                continue
            if not same_source(item.source, item.source_file, *self._identity(chunk, titles)):
                result.rejected["source_mismatch"] += 1
                logger.info("Generated question names a different source", extra={"chunk_id": chunk.id})
                continue
            seen.append(item.question)
            pending.append((item, chunk, check.match.score))  # type: ignore[union-attr]

        # All questions of this chunk are verified together: one blind source check and one quality
        # review per chunk instead of two requests per question (same checks, same strictness).
        candidates: list[_Candidate] = []
        for item, chunk, score, verified in await self._verify_all(pending, result):
            reference, kind = self._ref(chunk, titles)
            candidates.append(_Candidate(item, chunk, reference, kind, {**verified, "excerpt_score": score}))
        if not candidates:
            return []
        known_topics = list((await session.execute(select(Topic))).scalars().all())
        stored: list[Question] = []
        for cand in candidates:
            # The generator already picked the topic (from the existing topic list when one fits);
            # near-identical names are merged locally — no separate classification request.
            topic = await get_or_create_topic(session, cand.data.topic, known_topics)
            question = Question(
                question_text=cand.data.question.strip(),
                options={k: v.strip() for k, v in cand.data.options.items()},
                option_count=len(cand.data.options),
                correct_option=cand.data.correct_answer,
                explanation=cand.data.explanation.strip(),
                topic_id=topic.id if topic else None,
                difficulty=Difficulty(cand.data.difficulty),
                status=QuestionStatus.PENDING,
                origin=QuestionOrigin.AI,
                source_kind=cand.kind,
                source_material_id=cand.chunk.material_id,
                source_news_id=cand.chunk.news_id,
                source_chunk_id=cand.chunk.id,
                source_page=cand.chunk.page_label,
                source_reference=cand.reference,
                source_excerpt=cand.data.source_excerpt.strip(),
                text_hash=text_hash(cand.data.question),
                validation={**cand.verification, "model": self.llm.default_model},
                confidence=cand.verification.get("confidence"),
            )
            session.add(question)
            stored.append(question)
        await session.commit()
        return stored

    async def _hash_exists(self, session: AsyncSession, question: str) -> bool:
        stmt = select(Question.id).where(Question.text_hash == text_hash(question)).limit(1)
        return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def _source_verification(
        self, source_text: str, question: str, options: dict[str, str]
    ) -> SourceVerification:
        # Cached: re-checking the same question on the same text (e.g. admin presses twice) is free.
        return await structured_call(
            self.llm,
            prompts.SOURCE_VERIFICATION_SYSTEM,
            prompts.source_verification_user(source_text, question, options),
            SourceVerification,
            retries=self.settings.ai_max_retries,
            model=self.settings.validation_model,
            temperature=0.0,
            max_tokens=500,
            purpose="source_verification",
            cache_ttl=self.settings.ai_cache_ttl_seconds,
        )

    async def _topic_names(self, session: AsyncSession, limit: int = 40) -> list[str]:
        """Most used topic names, so the generator reuses them (keeps topics consistent)."""
        stmt = (
            select(Topic.name)
            .join(Question, Question.topic_id == Topic.id)
            .group_by(Topic.id, Topic.name)
            .order_by(func.count(Question.id).desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def _verify_batch(self, chunk: SourceChunk, items: list[GeneratedQuestion]) -> list[SourceVerification]:
        """Blind answers for several questions on the same source text in ONE request; anything the
        batch does not answer cleanly is checked again one by one (never skipped)."""
        answers: dict[int, SourceVerification] = {}
        if len(items) > 1:
            try:
                batch = await structured_call(
                    self.llm,
                    prompts.SOURCE_VERIFICATION_BATCH_SYSTEM,
                    prompts.source_verification_batch_user(chunk.text, [(q.question, q.options) for q in items]),
                    SourceVerificationBatch,
                    retries=self.settings.ai_max_retries,
                    model=self.settings.validation_model,
                    temperature=0.0,
                    max_tokens=350 * len(items) + 200,
                    purpose="source_verification",
                    cache_ttl=self.settings.ai_cache_ttl_seconds,
                )
                answers = {r.index: r for r in batch.results if 0 <= r.index < len(items)}
            except AIResponseError as exc:
                logger.warning("Batch source verification failed; checking one by one", extra={"error": str(exc)[:200]})
        return [
            answers.get(i) or await self._source_verification(chunk.text, q.question, q.options)
            for i, q in enumerate(items)
        ]

    async def _quality_batch(self, items: list[GeneratedQuestion]) -> list[QualityValidation]:
        reviews: dict[int, QualityValidation] = {}
        rows = [(q.source_excerpt, q.question, q.options, q.correct_answer, q.explanation) for q in items]
        if len(items) > 1:
            try:
                batch = await structured_call(
                    self.llm,
                    prompts.QUESTION_VALIDATION_BATCH_SYSTEM,
                    prompts.question_validation_batch_user(rows),
                    QualityValidationBatch,
                    retries=self.settings.ai_max_retries,
                    model=self.settings.validation_model,
                    temperature=0.0,
                    max_tokens=400 * len(items) + 200,
                    purpose="quality_validation",
                    cache_ttl=self.settings.ai_cache_ttl_seconds,
                )
                reviews = {r.index: r for r in batch.results if 0 <= r.index < len(items)}
            except AIResponseError as exc:
                logger.warning("Batch quality review failed; reviewing one by one", extra={"error": str(exc)[:200]})
        out = []
        for i, row in enumerate(rows):
            review = reviews.get(i)
            if review is None:
                review = await structured_call(
                    self.llm,
                    prompts.QUESTION_VALIDATION_SYSTEM,
                    prompts.question_validation_user(*row),
                    QualityValidation,
                    retries=self.settings.ai_max_retries,
                    model=self.settings.validation_model,
                    temperature=0.0,
                    max_tokens=600,
                    purpose="quality_validation",
                    cache_ttl=self.settings.ai_cache_ttl_seconds,
                )
            out.append(review)
        return out

    async def _verify_all(
        self, pending: list[tuple[GeneratedQuestion, SourceChunk, float]], result: GenerationResult
    ) -> list[tuple[GeneratedQuestion, SourceChunk, float, dict]]:
        """Blind source verification (grouped by chunk) → quality review (one batch) → explanation fix."""
        if not pending:
            return []
        by_chunk: dict[int, list[int]] = {}
        for index, (_, chunk, _) in enumerate(pending):
            by_chunk.setdefault(chunk.id, []).append(index)
        verifications: dict[int, SourceVerification] = {}
        for indexes in by_chunk.values():
            chunk = pending[indexes[0]][1]
            for index, verification in zip(
                indexes, await self._verify_batch(chunk, [pending[i][0] for i in indexes]), strict=True
            ):
                verifications[index] = verification
        # 1) Blind answer: an independent pass must reach the same key using only the source.
        passed: list[int] = []
        for index, (item, _, _) in enumerate(pending):
            verification = verifications[index]
            if not verification.supported or verification.answer is None:
                result.rejected["source_not_supporting"] += 1
            elif verification.answer != item.correct_answer:
                result.rejected["verifier_disagrees"] += 1
            elif verification.confidence < self.settings.ai_min_confidence:
                result.rejected["low_confidence"] += 1
            else:
                passed.append(index)
        if not passed:
            return []
        # 2) Quality review with the answer key.
        reviews = await self._quality_batch([pending[i][0] for i in passed])
        accepted = []
        for index, quality in zip(passed, reviews, strict=True):
            item, chunk, score = pending[index]
            if (
                quality.verdict != "accept"
                or not quality.single_correct
                or not quality.understandable
                or not quality.options_meaningful
                or quality.ambiguous
            ):
                result.rejected["quality_rejected"] += 1
                logger.info(
                    "Question rejected by quality validation", extra={"issues": "; ".join(quality.issues)[:300]}
                )
                continue
            explanation_regenerated = False
            if not quality.explanation_consistent:
                new_explanation = await self.regenerate_explanation(
                    chunk.text, item.question, item.options, item.correct_answer
                )
                if new_explanation is None:
                    result.rejected["explanation_unsupported"] += 1
                    continue
                item.explanation = new_explanation
                explanation_regenerated = True
            verification = verifications[index]
            accepted.append(
                (
                    item,
                    chunk,
                    score,
                    {
                        "confidence": verification.confidence,
                        "verifier_answer": verification.answer,
                        "verifier_evidence": verification.evidence[:500],
                        "quality_issues": quality.issues[:5],
                        "explanation_regenerated": explanation_regenerated,
                    },
                )
            )
        return accepted


def _norm_source(value: str) -> str:
    value = value.strip().casefold()
    for ext in (".pdf", ".docx", ".doc", ".txt", ".md"):
        value = value.removesuffix(ext)
    return " ".join(value.replace("_", " ").replace("-", " ").split())


def same_source(claimed_name: str, claimed_file: str, real_name: str, real_file: str) -> bool:
    """Does the document the AI named match the chunk's real document (name or file name)?"""
    real = {_norm_source(real_name), _norm_source(real_file)} - {""}
    claimed = {_norm_source(claimed_name), _norm_source(claimed_file)} - {""}
    if not real or not claimed:
        return False
    return any(c == r or fuzz.ratio(c, r) >= 90 for c in claimed for r in real)


async def get_or_create_topic(session: AsyncSession, name: str, existing: list[Topic] | None = None) -> Topic | None:
    clean = " ".join(name.split()).strip(" .,-")[:120]
    normalized = normalize_topic(clean)
    if not normalized:
        return None
    if existing is None:
        existing = list((await session.execute(select(Topic))).scalars().all())
    for topic in existing:
        if topic.normalized_name == normalized:
            return topic
    if existing:
        match = process.extractOne(normalized, {t.id: t.normalized_name for t in existing}, scorer=fuzz.ratio)
        if match and match[1] >= 88:
            return next(t for t in existing if t.id == match[2])
    topic = Topic(name=clean[:1].upper() + clean[1:], normalized_name=normalized)
    session.add(topic)
    await session.flush()
    existing.append(topic)
    return topic
