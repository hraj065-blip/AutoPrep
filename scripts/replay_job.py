import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config
from app.services.jobs import _df_bytes, get_job, replay_job


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    job = get_job(Config.DATABASE_PATH, args.job_id)
    if not job:
        raise SystemExit("Job not found")
    frame = replay_job(Config.DATABASE_PATH, args.job_id)
    expected = job["candidate"]
    actual_hash = hashlib.sha256(_df_bytes(frame)).hexdigest()
    expected_hash = hashlib.sha256(expected).hexdigest() if expected else None
    print({"status": "MATCH" if actual_hash == expected_hash else "DIVERGED",
           "replay_hash": actual_hash, "candidate_hash": expected_hash, "rows": len(frame)})


if __name__ == "__main__":
    main()
