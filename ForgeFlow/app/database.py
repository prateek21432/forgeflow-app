"""SQLAlchemy records for ownership, durable state, and the development audit trail."""

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Timestamp:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Timestamp, Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(40), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))


class Project(Timestamp, Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    workspace_path: Mapped[str] = mapped_column(String(600))
    branch: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(40), default="ready")
    current_task: Mapped[str] = mapped_column(Text, default="")
    demo_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    demo_status: Mapped[str] = mapped_column(String(40), default="stopped")
    remote_url: Mapped[str] = mapped_column(String(600), default="")
    active_run_id: Mapped[int | None] = mapped_column(nullable=True)


class Run(Timestamp, Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    request: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="queued")
    current_agent: Mapped[str] = mapped_column(String(40), default="Supervisor")
    iteration: Mapped[int] = mapped_column(default=0)
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Task(Timestamp, Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="planned")


class Event(Timestamp, Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    agent: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(60))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class ToolCall(Timestamp, Base):
    __tablename__ = "tool_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    agent: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(80))
    arguments: Mapped[dict] = mapped_column(JSON)
    result: Mapped[str] = mapped_column(Text)
    success: Mapped[bool] = mapped_column(Boolean)


class TestRun(Timestamp, Base):
    __tablename__ = "test_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    phase: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20))
    total: Mapped[int] = mapped_column(default=0)
    passed: Mapped[int] = mapped_column(default=0)
    failed: Mapped[int] = mapped_column(default=0)
    skipped: Mapped[int] = mapped_column(default=0)
    cases: Mapped[list] = mapped_column(JSON, default=list)
    output: Mapped[str] = mapped_column(Text, default="")


class Checkpoint(Timestamp, Base):
    __tablename__ = "checkpoints"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), unique=True)
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="validated")
    test_count: Mapped[int] = mapped_column()
    passed_tests: Mapped[int] = mapped_column()
    failed_tests: Mapped[int] = mapped_column(default=0)
    commit_hash: Mapped[str] = mapped_column(String(64))
    branch: Mapped[str] = mapped_column(String(100))
    pushed: Mapped[bool] = mapped_column(Boolean, default=False)
    report: Mapped[dict] = mapped_column(JSON, default=dict)


class GitCommit(Timestamp, Base):
    __tablename__ = "git_commits"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    checkpoint_id: Mapped[int] = mapped_column(ForeignKey("checkpoints.id"))
    commit_hash: Mapped[str] = mapped_column(String(64))
    branch: Mapped[str] = mapped_column(String(100))
    push_status: Mapped[str] = mapped_column(String(40), default="not_configured")
    error: Mapped[str] = mapped_column(Text, default="")


engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False}
    if settings.database_url.startswith("sqlite")
    else {"connect_timeout": 5},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
