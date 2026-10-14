from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://postgres:admin@127.0.0.1:5432/eldercare"
    JWT_SECRET: str = "change-me-secret-key-elder-care-hackathon"
    JWT_EXPIRE_MINUTES: int = 1440
    CORS_ORIGINS: str = "*"
    TZ_LOCAL: str = "Asia/Jakarta"
    SIM_TICK_SECONDS: int = 3
    INACTIVITY_MIN: int = 90
    LLM_PROVIDER: str = "gemini"
    LLM_API_KEY: Optional[str] = ""
    LLM_MODEL: Optional[str] = ""
    SUPABASE_URL: Optional[str] = ""
    SUPABASE_SERVICE_KEY: Optional[str] = ""
    SUPABASE_BUCKET: str = "documents"
    PORT: int = 8000

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
