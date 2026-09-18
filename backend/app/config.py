from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchored to backend/ regardless of the process's cwd. `make up` starts the API
# and worker from the repo root; pytest and manual runs use backend/ as cwd —
# a relative ".env"/"./uploads" resolved to two different locations depending
# on which, so backend/.env was silently never being read by `make up`'s
# processes (ANTHROPIC_API_KEY, if only set there, was invisible to them; other
# settings happened to have defaults matching docker-compose's values, so
# nothing looked wrong until API key selection exposed it).
BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BACKEND_DIR / ".env"), extra="ignore")

    database_url: str = "postgresql+psycopg://invoice:invoice@localhost:5432/invoice_intelligence"
    redis_url: str = "redis://localhost:6379/0"
    anthropic_api_key: str = ""

    upload_dir: str = str(BACKEND_DIR / "uploads")
    inbox_dir: str = str(BACKEND_DIR.parent / "inbox")

    extraction_model: str = "claude-sonnet-4-6"


settings = Settings()
