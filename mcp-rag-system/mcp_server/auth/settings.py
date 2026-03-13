from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    oauth_secret_key: str = "change-me-in-production-use-a-long-random-string"
    oauth_token_expire_minutes: int = 60
    mcp_server_name: str = "rag-mcp-server"
    allowed_origins: str = "*"
    rag_service_url: str = "http://rag_service:8001"
    admin_username: str = "admin"
    admin_password: str = "changeme"

    # ---- GCP / Cloud Run ----
    # Set to true when running on Cloud Run to enable:
    #   - Identity Token auth for internal service-to-service calls
    #   - Redis-backed OAuth state (requires redis_url)
    cloud_run_env: bool = False

    # Cloud Memorystore Redis URL, e.g.:
    #   redis://10.0.0.3:6379  (internal VPC IP from Cloud Memorystore)
    # When unset, OAuth state uses in-memory dicts (fine for local dev).
    redis_url: str | None = None

    # GCP project used for Vertex AI and other GCP SDK calls.
    gcp_project_id: str | None = None

    class Config:
        env_file = ".env"


settings = Settings()
