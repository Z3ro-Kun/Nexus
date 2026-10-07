"""Check connectivity to the PostgreSQL instance configured in .env.

Usage (from backend/):  uv run python ../scripts/check_db.py
"""

import asyncio
import sys

from app.core.config import get_settings
from app.persistence.database import Database


async def main() -> int:
    settings = get_settings()
    database = Database(settings.database_url)
    try:
        await database.ping()
    except Exception as exc:  # noqa: BLE001 - report any connection failure
        print(f"FAIL  {settings.database_url}: {exc}")
        return 1
    finally:
        await database.dispose()
    print(f"OK    {settings.database_url}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
