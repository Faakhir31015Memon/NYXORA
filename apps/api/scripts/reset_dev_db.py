"""Reset the local development database.

Dev-only. It refuses to run unless the target looks like a disposable local
file, because the alternative is deleting someone's real memory store.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db.models import init_db, make_engine  # noqa: E402


def main() -> int:
    settings = get_settings()
    target = Path(settings.db_path)
    if str(target) != ":memory:" and not (
        target.suffix == ".db" and "localhost" not in str(target)
    ):
        print(f"refusing to reset a non-dev database: {target}", file=sys.stderr)
        return 1

    if target.exists():
        for extra in (target, *target.parent.glob(target.name + "*")):
            if extra.is_file():
                extra.unlink()
                print(f"removed {extra}")
    else:
        print(f"no database at {target}")

    init_db(make_engine(target))
    print(f"rebuilt {target} from the current schema")
    print("note: run 'alembic upgrade head' instead if you keep migration history")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
