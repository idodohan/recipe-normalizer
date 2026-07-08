from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RN_")

    database_url: str = "postgresql+psycopg://rn:rn@localhost:5432/rn"
    session_ttl_hours: int = 24 * 30
    cookie_secure: bool = False
    file_store_root: str = "./filestore"
    llm_model: str = "claude-opus-4-8"
    llm_fast_model: str = "claude-haiku-4-5"
    llm_max_retries: int = 3
    llm_timeout_s: float = 120.0
    job_cost_cap_usd: float = 1.50
    admin_emails: str = ""
    rate_limit_auth_per_minute: int = 10
    rate_limit_ingest_per_hour: int = 60
    cors_origins: str = "http://localhost:5173"
    netguard_allow_hosts: str = ""


settings = Settings()
