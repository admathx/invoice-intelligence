from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://invoice:invoice@localhost:5432/invoice_intelligence"
    redis_url: str = "redis://localhost:6379/0"
    anthropic_api_key: str = ""

    upload_dir: str = "./uploads"
    inbox_dir: str = "../inbox"

    extraction_model: str = "claude-sonnet-4-6"


settings = Settings()
