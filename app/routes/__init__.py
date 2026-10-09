from flask import Blueprint

bp = Blueprint("main", __name__)

from app.routes import views  # noqa: E402,F401
