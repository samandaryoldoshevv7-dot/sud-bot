from sqlalchemy import func, select, text

from app.models import Material, MaterialStatus, Question, QuestionStatus, SourceChunk, Topic
from app.rag.retriever import Retriever
from app.rag.vector_store import SourceScope, detect_backend, fulltext_search, vector_search
from app.services.question_generation import GenerationRequest, QuestionGenerator
from app.utils.text import normalize_for_match
from tests.factories import HashEmbeddings, ScriptedLLM, make_material_with_text


async def test_material_processing_creates_chunks_and_embeddings(session_maker):
    mid = await make_material_with_text(session_maker)
    async with session_maker() as session:
        material = await session.get(Material, mid)
        assert material.status == MaterialStatus.READY
        assert material.chunk_count >= 1 and material.char_count > 500
        backend = await detect_backend(session, refresh=True)
        embedded = (
            await session.execute(
                text("SELECT count(*) FROM source_chunks WHERE embedding IS NOT NULL AND material_id = :m"), {"m": mid}
            )
        ).scalar_one()
        assert backend in ("pgvector", "array")
        assert embedded == material.chunk_count


async def test_vector_and_fulltext_search(session_maker):
    mid = await make_material_with_text(session_maker)
    emb = HashEmbeddings()
    async with session_maker() as session:
        scope = SourceScope(material_ids=[mid])
        qvec = await emb.embed_query("arxivga topshirish besh yil saqlanadi")
        hits = await vector_search(session, qvec, scope, 3)
        assert hits, "vector search returned nothing"
        best = await session.get(SourceChunk, hits[0][0])
        assert "arxiv" in best.text.lower()
        fts = await fulltext_search(session, "apellyatsiya shikoyati", scope, 3)
        assert fts and "Apellyatsiya" in (await session.get(SourceChunk, fts[0][0])).text
        ranked = await Retriever(emb).search(session, "sud majlisi bayonnomasi", scope, 3)
        assert ranked


async def test_failed_processing_marks_material_failed(session_maker):
    from app.models import FileType
    from app.services import materials as material_service

    async with session_maker() as session:
        material = await material_service.create_material(
            session, title="Bo'sh", file_type=FileType.TEXT, uploaded_by_id=None, raw_text="   "
        )
    result = await material_service.process_material(session_maker, material.id, None, HashEmbeddings())
    assert not result.ok and result.error_code in ("no_source", "empty_document")
    async with session_maker() as session:
        refreshed = await session.get(Material, material.id)
        assert refreshed.status == MaterialStatus.FAILED and refreshed.error_message


async def test_generation_pipeline_stores_grounded_questions(session_maker):
    mid = await make_material_with_text(session_maker)
    llm = ScriptedLLM()
    async with session_maker() as session:
        gen = QuestionGenerator(llm, Retriever(HashEmbeddings()))
        result = await gen.generate(session, GenerationRequest(scope=SourceScope(material_ids=[mid]), count=3))
        assert result.created == 3, result.rejected
        rows = (await session.execute(select(Question))).scalars().all()
        for q in rows:
            assert q.status == QuestionStatus.PENDING
            assert q.source_material_id == mid and q.source_chunk_id is not None
            chunk = await session.get(SourceChunk, q.source_chunk_id)
            assert normalize_for_match(q.source_excerpt) in normalize_for_match(chunk.text)
            assert q.source_reference.startswith("Yo'riqnoma")
            assert q.validation["verifier_answer"] == q.correct_option
            assert q.topic_id is not None
        assert (await session.execute(select(func.count(Topic.id)))).scalar_one() == 1
    assert {"generation", "verification", "quality", "topics"} <= set(llm.calls)


async def test_generation_rejects_when_verifier_disagrees(session_maker):
    mid = await make_material_with_text(session_maker)
    async with session_maker() as session:
        gen = QuestionGenerator(ScriptedLLM(verifier_agrees=False), Retriever(HashEmbeddings()))
        result = await gen.generate(
            session, GenerationRequest(scope=SourceScope(material_ids=[mid]), count=2, max_llm_rounds=2)
        )
        assert result.created == 0
        assert result.rejected["verifier_disagrees"] >= 1
        assert (await session.execute(select(func.count(Question.id)))).scalar_one() == 0


async def test_generation_rejects_invented_excerpt(session_maker):
    mid = await make_material_with_text(session_maker)
    async with session_maker() as session:
        gen = QuestionGenerator(ScriptedLLM(bad_excerpt=True), Retriever(HashEmbeddings()))
        result = await gen.generate(
            session, GenerationRequest(scope=SourceScope(material_ids=[mid]), count=2, max_llm_rounds=2)
        )
        assert result.created == 0
        assert result.rejected["excerpt_not_found_in_source"] >= 1


async def test_generation_recovers_from_invalid_json(session_maker):
    mid = await make_material_with_text(session_maker)
    llm = ScriptedLLM(invalid_first=True)
    async with session_maker() as session:
        gen = QuestionGenerator(llm, Retriever(HashEmbeddings()))
        result = await gen.generate(session, GenerationRequest(scope=SourceScope(material_ids=[mid]), count=1))
        assert result.created == 1


async def test_fulltext_only_when_embeddings_disabled(session_maker):
    from app.rag.embeddings import NullEmbeddingProvider

    mid = await make_material_with_text(session_maker, embeddings=NullEmbeddingProvider())
    async with session_maker() as session:
        ranked = await Retriever(NullEmbeddingProvider()).search(session, "arxiv", SourceScope(material_ids=[mid]), 3)
        assert ranked
        gen = QuestionGenerator(ScriptedLLM(), Retriever(NullEmbeddingProvider()))
        result = await gen.generate(session, GenerationRequest(scope=SourceScope(material_ids=[mid]), count=1))
        assert result.created == 1
