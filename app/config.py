"""Application settings, read from environment variables (and a local .env file in development)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str                          # e.g. postgresql+psycopg2://user:password@host:5432/dbname
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:3b"
    ollama_timeout: float = 120                # seconds for one model call


settings = Settings()
