
"""Application settings, read from environment variables and local .env."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    llm_provider: str = "ollama"
    gemini_model: str = "gemini-2.5-flash-lite"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:3b"
    ollama_timeout: float = 120

    # --- demo console (mock gateway + dashboard) ---
    demo_mode: bool = False          # DEMO_MODE=true switches on the mock-gateway WRITE endpoints (/gateway/*)
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"    # comma-separated browser origins allowed to call the API
    demo_currency: str = "INR"       # the dashboard only sums amounts in this currency
    demo_access_token: str = ""      # server-side access code; when set, protected endpoints need header X-Demo-Token
    demo_allow_open_access: bool = False    # LOCAL DEV ONLY: allow DEMO_MODE without an access code
    demo_max_llm_runs_per_hour: int = 30    # per-process budget of model-calling requests while DEMO_MODE is on
    demo_max_events_per_hour: int = 60      # per-process budget of synthetic gateway events
    demo_batch_enabled: bool = False        # the batch endpoint is off unless explicitly enabled
    demo_batch_max: int = 3                 # largest batch when enabled


settings = Settings()
