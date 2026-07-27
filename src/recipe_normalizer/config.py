from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RN_")

    database_url: str = "postgresql+psycopg://rn:rn@localhost:5432/rn"
    session_ttl_hours: int = 24 * 30
    cookie_secure: bool = False
    file_store_root: str = "./filestore"
    # "auto" picks openrouter when OPENROUTER_API_KEY is set, else anthropic
    # when ANTHROPIC_API_KEY is set, else openrouter (fails with a clear error
    # at call time). Set RN_LLM_PROVIDER explicitly to pin one.
    llm_provider: str = "auto"
    # Empty = per-provider defaults (see llm/client.py _PROVIDER_DEFAULT_MODELS):
    # openrouter -> openrouter/free (auto-router over free models),
    # anthropic  -> claude-opus-4-8 / claude-haiku-4-5.
    llm_model: str = ""
    llm_fast_model: str = ""
    llm_max_retries: int = 3
    llm_timeout_s: float = 120.0
    job_cost_cap_usd: float = 1.50
    ai_request_cost_cap_usd: float = 0.50
    # NOTE: there is deliberately no admin-email allow-list. Admin is granted
    # only via `python -m recipe_normalizer.users.make_admin <email>` — see
    # users/service.register.
    rate_limit_auth_per_minute: int = 10
    rate_limit_ingest_per_hour: int = 60
    cors_origins: str = "http://localhost:5173"
    netguard_allow_hosts: str = ""


settings = Settings()
