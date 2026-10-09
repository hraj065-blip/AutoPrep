import os
import secrets
from pathlib import Path


class Config:
    IS_VERCEL = bool(os.environ.get("VERCEL"))
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"
    INSTANCE_PATH = Path(os.environ.get("PREPPILOT_INSTANCE_PATH", "instance"))
    DATABASE_PATH = os.environ.get("PREPPILOT_DATABASE_PATH", str(INSTANCE_PATH / "preppilot.sqlite3"))
    DATABASE_URL = os.environ.get("DATABASE_URL", "")
    SOURCE_PATH = str(INSTANCE_PATH / "sources")
    ARTIFACT_PATH = str(INSTANCE_PATH / "artifacts")
    # Vercel Functions reject request bodies above 4.5 MB before Flask handles them.
    MAX_CONTENT_LENGTH = int(os.environ.get(
        "PREPPILOT_MAX_UPLOAD_MB", "2" if os.environ.get("VERCEL") else "25"
    )) * 1024 * 1024
    MAX_ROWS = int(os.environ.get("PREPPILOT_MAX_ROWS", "250000"))
    MAX_COLUMNS = int(os.environ.get("PREPPILOT_MAX_COLUMNS", "500"))
    MAX_SHEETS = int(os.environ.get("PREPPILOT_MAX_SHEETS", "30"))
    MAX_AGENT_STEPS = int(os.environ.get("PREPPILOT_MAX_AGENT_STEPS", "5"))
    MAX_OPERATIONS = int(os.environ.get("PREPPILOT_MAX_OPERATIONS", "50"))
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("PREPPILOT_SECURE_COOKIES", "1" if os.environ.get("VERCEL") else "0") == "1"
