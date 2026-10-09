"""A crash of the embedding model (out of memory) must not fail small files or kill the bot again."""

import pytest

from app.config import get_settings
from app.keyboards.callbacks import AdminCB
from app.models import BotSetting, FileType, Material, MaterialStatus
from app.rag.embeddings import FastEmbedProvider, NullEmbeddingProvider, get_embedding_provider, set_embedding_provider
from app.services import embedding_guard
from app.services import materials as material_service
from tests.factories import make_employee
from tests.telegram_mock import callback_update
from tests.test_group_flow import tg  # noqa: F401  (fixture)

ADMIN = 1000
TEXT = (
    "SUDLAR TO'G'RISIDA\n\n"
    "1-modda. Sudlar odil sudlovni amalga oshiradi va qonunga bo'ysunadi.\n\n"
    "2-modda. Sudyalar mustaqil bo'lib, faqat qonunga bo'ysunadilar va daxlsizdirlar.\n"
)


class _LocalModel(FastEmbedProvider):
    """Stands for the local ONNX model; records whether the crash marker was set while it worked."""

    def __init__(self, session_maker):
        super().__init__(get_settings())
        self.session_maker = session_maker
        self.marker_seen: list[bool] = []

    async def embed_documents(self, texts):
        async with self.session_maker() as session:
            self.marker_seen.append(await session.get(BotSetting, embedding_guard.RUNNING_KEY) is not None)
        return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]

    async def warmup(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _restore_provider():
    yield
    set_embedding_provider(None)


async def _material(session_maker, status=MaterialStatus.UPLOADED) -> int:
    async with session_maker() as session:
        material = await material_service.create_material(
            session, title="Sudlar to'g'risida", file_type=FileType.TEXT, uploaded_by_id=None, raw_text=TEXT
        )
        material.status = status
        await session.commit()
        return material.id


async def test_marker_is_set_only_while_the_model_works(session_maker):
    model = _LocalModel(session_maker)
    mid = await _material(session_maker)
    result = await material_service.process_material(session_maker, mid, embeddings=model)
    assert result.ok and result.embedded
    assert model.marker_seen and all(model.marker_seen)
    async with session_maker() as session:
        assert await session.get(BotSetting, embedding_guard.RUNNING_KEY) is None


async def test_bot_killed_while_embedding_switches_semantic_search_off(tg, session_maker):  # noqa: F811
    """The real case: a 71 KB file was marked ❌ "crashed twice" because the model ran out of memory."""
    from app.handlers.admin.materials import resume_interrupted_materials

    async with session_maker() as session:
        await make_employee(session, ADMIN, "Admin Bosh")
    mid = await _material(session_maker, MaterialStatus.PROCESSING)
    async with session_maker() as session:  # the process died twice while embedding this file
        session.add(BotSetting(key=embedding_guard.RUNNING_KEY, value=True))
        session.add(BotSetting(key=f"material_resume:{mid}", value=1))
        await session.commit()

    async with session_maker() as session:
        assert await embedding_guard.check_after_restart(session) is True
        assert await embedding_guard.is_disabled(session)
    embedding_guard.switch_off()
    assert isinstance(get_embedding_provider(), NullEmbeddingProvider)

    assert await resume_interrupted_materials(tg.bot, session_maker, delay=0) == 1
    async with session_maker() as session:
        material = await session.get(Material, mid)
        assert material.status == MaterialStatus.READY and material.chunk_count > 0  # full-text search
        assert await embedding_guard.check_after_restart(session) is False  # nothing left behind
    assert any("qayta o'qildi va tayyor" in m.text for m in tg.session.sent_to(ADMIN))


async def test_admin_can_switch_semantic_search_on_again(tg, session_maker):  # noqa: F811
    async with session_maker() as session:
        await make_employee(session, ADMIN, "Admin Bosh")
        session.add(BotSetting(key=embedding_guard.DISABLED_KEY, value=True))
        await session.commit()
    embedding_guard.switch_off()

    await tg(callback_update(ADMIN, AdminCB(s="set").pack()))
    assert any("Aqlli qidiruv o'chirilgan" in x for x in tg.session.texts())

    model = _LocalModel(session_maker)
    set_embedding_provider(model)
    import app.services.embedding_guard as guard_module

    real_get = guard_module.get_embedding_provider
    guard_module.get_embedding_provider = lambda: model
    try:
        tg.session.clear()
        await tg(callback_update(ADMIN, AdminCB(s="set_emb").pack()))
    finally:
        guard_module.get_embedding_provider = real_get
    assert any("Aqlli qidiruv yoqildi" in x for x in tg.session.texts())
    async with session_maker() as session:
        assert not await embedding_guard.is_disabled(session)
        assert await session.get(BotSetting, embedding_guard.RUNNING_KEY) is None
