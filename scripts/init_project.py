import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config  # noqa: E402
from app.persistence.db import initialize  # noqa: E402


def main() -> None:
    Path("instance").mkdir(exist_ok=True)
    Path("data/benchmarks").mkdir(parents=True, exist_ok=True)
    database = Config.DATABASE_URL or Config.DATABASE_PATH
    initialize(database)
    print(f"PrepPilot initialized using {'PostgreSQL' if database.startswith(('postgres://', 'postgresql://')) else Path(database).resolve()}")


if __name__ == "__main__":
    main()
