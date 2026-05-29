from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

FactKind = Literal[
    "health", "preference", "goal", "project", "relationship", "event", "insight", "routine"
]


class Profile(BaseModel):
    user_id: int
    display_name: str | None = None
    timezone: str = "Asia/Bangkok"
    wake_window: str | None = None
    sleep_window: str | None = None
    goals: list[Any] = Field(default_factory=list)
    projects: list[Any] = Field(default_factory=list)
    preferences: dict[str, Any] = Field(default_factory=dict)
    onboarding_completed_at: datetime | None = None
    paused_until: datetime | None = None


class Fact(BaseModel):
    id: int | None = None
    user_id: int
    kind: FactKind
    content: str
    confidence: float = 0.8
    source_message_id: int | None = None
    superseded_by: int | None = None
    created_at: datetime | None = None
    last_referenced_at: datetime | None = None


class ExtractedFact(BaseModel):
    """Структура, которую возвращает Haiku при извлечении фактов."""

    kind: FactKind
    content: str
    confidence: float = Field(0.8, ge=0.0, le=1.0)
    supersedes_id: int | None = None


class ConversationMsg(BaseModel):
    id: int | None = None
    role: Literal["user", "assistant", "system"]
    content: str
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None


class TaskItem(BaseModel):
    id: int | None = None
    user_id: int
    title: str
    details: str | None = None
    project: str | None = None
    status: Literal["open", "doing", "done", "dropped"] = "open"
    priority: int = 3
    due_at: datetime | None = None
    remind_at: datetime | None = None
    postponed_count: int = 0
    completed_at: datetime | None = None
    created_at: datetime | None = None


class DiaryEntry(BaseModel):
    id: int | None = None
    user_id: int
    entry_date: date
    mood: int | None = None
    energy: int | None = None
    raw_text: str
    structured: dict[str, Any] | None = None
    created_at: datetime | None = None


class Habit(BaseModel):
    id: int | None = None
    user_id: int
    name: str
    cadence: str  # 'daily' | 'weekly:3' | 'cron:0 */3 * * *'
    target: dict[str, Any] | None = None
    active: bool = True


class ContextBundle(BaseModel):
    """Все данные, которые Memory собирает для одного запроса к LLM."""

    profile: Profile
    active_tasks: list[TaskItem]
    recent_conversation: list[ConversationMsg]
    relevant_facts: list[Fact]
    relevant_diary: list[DiaryEntry]
    today_snapshot: dict[str, Any]
