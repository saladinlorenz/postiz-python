from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Postiz Py"
    frontend_url: str = "http://localhost:8000"
    backend_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./postiz.db"
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_expiration_days: int = 7
    upload_directory: str = "./uploads"
    storage_provider: str = "local"

    linkedin_client_id: str = ""
    linkedin_client_secret: str = ""
    medium_api_key: str = ""
    telegram_bot_token: str = ""
    discord_client_id: str = ""
    discord_client_secret: str = ""
    discord_bot_token: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    slack_client_id: str = ""
    slack_client_secret: str = ""
    mastodon_client_id: str = ""
    mastodon_client_secret: str = ""
    mastodon_url: str = ""
    pinterest_client_id: str = ""
    pinterest_client_secret: str = ""
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    facebook_app_id: str = ""
    facebook_app_secret: str = ""
    x_api_key: str = ""
    x_api_secret: str = ""
    strip_links_from_x_posts: bool = False
    tiktok_client_id: str = ""
    tiktok_client_secret: str = ""
    threads_app_id: str = ""
    threads_app_secret: str = ""
    tumblr_client_id: str = ""
    tumblr_client_secret: str = ""
    vk_id: str = ""
    kick_client_id: str = ""
    kick_client_secret: str = ""
    twitch_client_id: str = ""
    twitch_client_secret: str = ""

    scheduler_interval_seconds: int = 10
    pending_check_interval_seconds: int = 20
    pending_max_checks: int = 90
    refresh_margin_seconds: int = 300


settings = Settings()
