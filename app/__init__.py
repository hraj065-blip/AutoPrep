import os

from flask import Flask
from flask.cli import load_dotenv

load_dotenv()

from app.config import Config  # noqa: E402
from app.persistence.db import initialize  # noqa: E402
from app.routes import bp  # noqa: E402


def create_app(config_object: type[Config] = Config) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)
    app.config.from_pyfile("config.py", silent=True)
    if os.environ.get("VERCEL"):
        required = ["SECRET_KEY", "PREPPILOT_ACCESS_PASSWORD", "DATABASE_URL"]
        missing = [key for key in required if not os.environ.get(key)]
        if missing:
            raise RuntimeError("Missing required Vercel environment variables: " + ", ".join(missing))
        if not app.config["DATABASE_URL"].startswith(("postgres://", "postgresql://")):
            raise RuntimeError("Vercel deployments require a durable PostgreSQL DATABASE_URL.")
    initialize(app.config["DATABASE_URL"] or app.config["DATABASE_PATH"])
    app.register_blueprint(bp)
    return app
