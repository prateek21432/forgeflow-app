"""Tests use an isolated database/workspace and never spend model quota."""

import os
import tempfile
from pathlib import Path

import pytest

TEST_ROOT = Path(tempfile.mkdtemp(prefix="forgeflow-tests-"))
os.environ["DATABASE_URL"] = "sqlite:///" + (TEST_ROOT / "platform.db").as_posix()
os.environ["WORKSPACE_ROOT"] = str(TEST_ROOT / "projects")
os.environ["RUNTIME_ROOT"] = str(TEST_ROOT / "runtime")
os.environ["SECRET_KEY"] = "test-only-secret-with-more-than-thirty-two-characters"
os.environ["EXECUTION_MODE"] = "local"
os.environ["LLM_MIN_INTERVAL"] = "0"

from app.config import settings
from app.database import Base, SessionLocal, engine


@pytest.fixture(autouse=True)
def clean_database(monkeypatch, tmp_path):
    # Run IDs restart with this database; cached QA files must not leak between tests.
    monkeypatch.setattr(settings, "runtime_root", tmp_path / "runtime")
    settings.prepare()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session
