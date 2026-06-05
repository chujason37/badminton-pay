from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str
    telegram_chat_id: int
    wise_webhook_secret: str = ""
    wise_api_token: str = ""      # Wise API token — needed to fetch sender/reference
    database_url: str = "sqlite:///./badminton.db"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
