from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RN_")

    database_url: str = "postgresql+psycopg://rn:rn@localhost:5432/rn"
    session_ttl_hours: int = 24 * 30
    file_store_root: str = "./filestore"


settings = Settings()
