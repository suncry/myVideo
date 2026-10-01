"""Manual smoke test for Qt Multimedia frame extraction."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as core
from desktop import extract_keyframes


def main() -> int:
    core.init_db()
    with core.connect() as conn:
        row = conn.execute("SELECT path FROM movies WHERE exists_now=1 ORDER BY id LIMIT 1").fetchone()
    if not row or not Path(row["path"]).is_file():
        print("SKIP: no indexed local video")
        return 0
    temp = tempfile.TemporaryDirectory()
    try:
        files = extract_keyframes(row["path"], Path(temp.name))
        print(f"OK: extracted {len(files)} distinct frames")
        outcome = 0 if files else 1
    except Exception as exc:
        print(f"ERROR: {exc}")
        outcome = 1
    temp.cleanup()
    return outcome


if __name__ == "__main__":
    raise SystemExit(main())
