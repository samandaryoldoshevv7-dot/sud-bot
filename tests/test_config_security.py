import logging
import re
import subprocess
from pathlib import Path

import pytest

from app.config import get_settings
from app.config.settings import normalize_database_url
from app.filters.admin import IsAdmin
from app.services.users import is_admin_telegram_id
from app.utils.logging import RedactingFilter
from tests.factories import tg_user

ROOT = Path(__file__).resolve().parents[1]


def test_admin_ids_parsed_from_env():
    assert get_settings().admin_ids == frozenset({1000, 1001})
    assert is_admin_telegram_id(1000)
    assert not is_admin_telegram_id(5555)


def test_admin_ids_reject_garbage(monkeypatch):
    from app.config.settings import Settings

    with pytest.raises(ValueError):
        Settings(bot_token="x", database_url="postgresql://a@b/c", admin_telegram_ids="123,abc")


@pytest.mark.parametrize(
    "url,expected,args",
    [
        ("postgresql://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db", {}),
        ("postgres://u:p@h/db", "postgresql+asyncpg://u:p@h/db", {}),
        ("postgresql://u:p@h/db?sslmode=require", "postgresql+asyncpg://u:p@h/db", {"ssl": "require"}),
        ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg://u:p@h/db", {}),
    ],
)
def test_database_url_normalisation(url, expected, args):
    assert normalize_database_url(url) == (expected, args)


class _Event:
    def __init__(self, user_id):
        self.from_user = tg_user(user_id)


async def test_is_admin_filter_is_server_side():
    flt = IsAdmin()
    assert await flt(_Event(1000)) is True
    assert await flt(_Event(42)) is False


def test_secrets_are_redacted_from_logs():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token=%s key=%s", ("123456:SECRET", "gsk_abc123"), None)
    record.extra_field = "contains gsk_abc123"
    RedactingFilter(["123456:SECRET", "gsk_abc123"]).filter(record)
    assert "SECRET" not in record.getMessage()
    assert "gsk_abc123" not in record.getMessage()
    assert record.extra_field == "contains ***"


def test_settings_repr_hides_secrets():
    s = get_settings()
    assert "TEST-TOKEN" not in repr(s)
    assert "devpass" not in repr(s)


def test_no_secrets_committed():
    """No .env file and nothing that looks like a real bot token / Groq key in tracked files."""
    assert not (ROOT / ".env").exists() or ".env" in (ROOT / ".gitignore").read_text()
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    files += [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py") if ".venv" not in p.parts]
    token_re = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")
    groq_re = re.compile(r"gsk_[A-Za-z0-9]{20,}")
    for name in set(files):
        path = ROOT / name
        if not path.is_file() or path.suffix in {".zip", ".png", ".pdf", ".xlsx"}:
            continue
        content = path.read_text(errors="ignore")
        assert not token_re.search(content), f"bot token in {name}"
        assert not groq_re.search(content), f"groq key in {name}"


def test_all_translation_keys_exist():
    import ast

    from app.locales.uz import TEXTS

    missing = []
    for path in (ROOT / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "t" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value not in TEXTS:
                    missing.append((path.name, arg.value))
    assert not missing


def test_dynamic_translation_keys_exist():
    from app.locales.uz import TEXTS
    from app.models import AttemptStatus, TestStatus, UserStatus
    from app.services.attempts import StartError
    from app.services.settings_service import SPECS

    keys = [f"attempt_status.{s.value}" for s in AttemptStatus]
    keys += [f"test_status.{s.value}" for s in TestStatus]
    keys += [f"user_status.{s.value}" for s in UserStatus]
    keys += [f"emp.start_error.{e.value}" for e in StartError]
    shown = [k for k, spec in SPECS.items() if spec.visible]  # hidden ones are changed elsewhere
    keys += [f"settings.{k}" for k in shown] + [f"settings.btn.{k}" for k in shown]
    keys += [f"difficulty.{d}" for d in ("easy", "medium", "hard", "mixed")]
    keys += [f"reveal.{r}" for r in ("immediate", "after", "never")]
    keys += [f"rank.period.{p}" for p in ("day", "week", "month", "all")]
    keys += [f"tests.part.btn.{k}" for k in ("all", "done", "prog", "exp", "none", "canc")]
    keys += [f"question_status.{s}" for s in ("pending", "approved", "rejected")]
    keys += [f"emp_admin.list.{s}" for s in ("all", "active", "pending", "inactive")]
    for code in ("unsupported_type", "unsupported_doc", "pdf_encrypted", "pdf_corrupted", "pdf_no_text",
                 "docx_corrupted", "empty_document", "too_large_text", "internal", "no_source"):  # fmt: skip
        keys.append(f"doc_error.{code}")
    for code in ("not_found", "not_draft", "generation_running", "question_count_mismatch", "not_all_approved",
                 "cannot_edit", "not_ready", "deadline_passed", "cannot_close", "cannot_extend",
                 "deadline_before_start", "cannot_delete"):  # fmt: skip
        keys.append(f"test_error.{code}")
    for reason in ("question_too_short", "question_too_long", "option_keys_invalid", "correct_not_in_options",
                   "empty_option", "duplicate_options", "near_duplicate_options", "option_too_long",
                   "forbidden_option_pattern", "answer_equals_question", "explanation_missing",
                   "excerpt_not_found_in_source", "model_rejected_source", "duplicate", "source_not_supporting",
                   "verifier_disagrees", "low_confidence", "quality_rejected", "explanation_unsupported", "ai_error"):  # fmt: skip
        keys.append(f"gen.reason.{reason}")
    assert [k for k in keys if k not in TEXTS] == []


def test_admin_ids_alias(monkeypatch):
    from app.config.settings import Settings

    monkeypatch.delenv("ADMIN_TELEGRAM_IDS", raising=False)
    monkeypatch.setenv("ADMIN_IDS", "42,43")
    assert Settings(_env_file=None).admin_ids == frozenset({42, 43})
