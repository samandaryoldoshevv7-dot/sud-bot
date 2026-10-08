"""Domain enumerations (stored as strings in the database)."""

from __future__ import annotations

import enum


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    EMPLOYEE = "employee"


class UserStatus(str, enum.Enum):
    PENDING = "pending"  # registered, waiting for admin approval
    ACTIVE = "active"
    INACTIVE = "inactive"  # deactivated (removed) by admin: hidden from lists, no access
    BLOCKED = "blocked"  # blocked by admin: stays in the lists, no access


class MaterialStatus(str, enum.Enum):
    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"


class FileType(str, enum.Enum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    MD = "md"
    TEXT = "text"  # pasted plain text


class Difficulty(str, enum.Enum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class TestDifficulty(str, enum.Enum):
    __test__ = False
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"
    MIXED = "mixed"


class QuestionStatus(str, enum.Enum):
    PENDING = "pending"  # generated & auto-validated, waiting for admin review
    APPROVED = "approved"
    REJECTED = "rejected"


class SourceKind(str, enum.Enum):
    MATERIAL = "material"
    NEWS = "news"


class QuestionOrigin(str, enum.Enum):
    AI = "ai"
    MANUAL = "manual"


class TestStatus(str, enum.Enum):
    __test__ = False
    DRAFT = "DRAFT"
    READY = "READY"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"


class GenerationStatus(str, enum.Enum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class AnswerReveal(str, enum.Enum):
    IMMEDIATE = "immediate"  # after each answer
    AFTER_COMPLETION = "after"  # detailed corrections after finishing
    NEVER = "never"  # only score


class AttemptStatus(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class ParticipationStatus(str, enum.Enum):
    """Derived per (employee, test). NOT_STARTED is the absence of any attempt."""

    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class DeliveryMode(str, enum.Enum):
    """Where employees answer: shared question posts inside a Telegram group, or a private chat."""

    GROUP = "group"
    PRIVATE = "private"


class TestAudience(str, enum.Enum):
    """Who receives a test: every active employee, members of a group, or chosen employees."""

    __test__ = False
    ALL = "all"
    GROUP = "group"
    USERS = "users"


class AssignmentStatus(str, enum.Enum):
    ASSIGNED = "assigned"
    REMOVED = "removed"  # admin took the test away from this employee


# Test durations the admin may choose (per employee, counted from the moment they press START).
DURATION_CHOICES = (6 * 3600, 12 * 3600, 24 * 3600, 48 * 3600)
DEFAULT_DURATION = 24 * 3600
