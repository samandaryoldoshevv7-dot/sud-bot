"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from app.models.enums import (
    AnswerReveal,
    AttemptStatus,
    DeliveryMode,
    Difficulty,
    FileType,
    GenerationStatus,
    MaterialStatus,
    ParticipationStatus,
    QuestionOrigin,
    QuestionStatus,
    SourceKind,
    TestDifficulty,
    TestStatus,
    UserRole,
    UserStatus,
)
from app.models.material import Material, News, SourceChunk
from app.models.question import Question, Topic
from app.models.setting import BotSetting
from app.models.test import (
    GroupQuestionMessage,
    GroupTestPost,
    QuestionOption,
    Test,
    TestAttempt,
    TestMaterial,
    TestQuestion,
    UserAnswer,
)
from app.models.user import Group, GroupMember, User

__all__ = [
    "AnswerReveal",
    "AttemptStatus",
    "BotSetting",
    "DeliveryMode",
    "Difficulty",
    "FileType",
    "GenerationStatus",
    "Group",
    "GroupMember",
    "GroupQuestionMessage",
    "GroupTestPost",
    "Material",
    "MaterialStatus",
    "News",
    "ParticipationStatus",
    "Question",
    "QuestionOption",
    "QuestionOrigin",
    "QuestionStatus",
    "SourceChunk",
    "SourceKind",
    "Test",
    "TestAttempt",
    "TestDifficulty",
    "TestMaterial",
    "TestQuestion",
    "TestStatus",
    "Topic",
    "User",
    "UserAnswer",
    "UserRole",
    "UserStatus",
]
