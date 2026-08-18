"""Environment configuration. Reads Backend/.env once at import time."""

import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is missing or empty in Backend/.env — fill it in before starting the app."
        )
    return value


# --- Database ---
DATABASE_URL = _require("DATABASE_URL")
DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "10"))

# --- Auth ---
JWT_SECRET = _require("JWT_SECRET")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "480"))

# --- App ---
APP_ENV = os.getenv("APP_ENV", "development")
