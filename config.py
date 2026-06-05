from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str
    telegram_chat_id: int
    wise_webhook_secret: str = ""
    wise_api_token: str = ""
    gmail_token_json: str = ""    # JSON string from gmail_setup.py — enables Gmail polling
    database_url: str = "sqlite:///./badminton.db"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
