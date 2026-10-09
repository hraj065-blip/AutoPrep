from pathlib import Path

import pytest

from app import create_app


@pytest.fixture
def app(tmp_path: Path):
    class TestConfig:
        TESTING = True
        SECRET_KEY = "test-secret"
        DEBUG = False
        DATABASE_PATH = str(tmp_path / "test.sqlite3")
        SOURCE_PATH = str(tmp_path / "sources")
        ARTIFACT_PATH = str(tmp_path / "artifacts")
        MAX_CONTENT_LENGTH = 1024 * 1024
        MAX_ROWS = 1000
        MAX_COLUMNS = 50
        MAX_SHEETS = 5
        MAX_AGENT_STEPS = 5
        MAX_OPERATIONS = 50

    return create_app(TestConfig)


@pytest.fixture
def client(app):
    return app.test_client()
