from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    oauth_secret_key: str = "change-me-in-production-use-a-long-random-string"
    oauth_token_expire_minutes: int = 60
    mcp_server_name: str = "rag-mcp-server"
    allowed_origins: str = "*"
    rag_service_url: str = "http://rag_service:8001"
    admin_username: str = "admin"
    admin_password: str = "changeme"

    class Config:
        env_file = ".env"


settings = Settings()
