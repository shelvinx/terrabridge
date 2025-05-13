from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, HttpUrl, SecretStr, field_validator, ValidationError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    tf_api_url: HttpUrl = Field(default="https://app.terraform.io", env="TF_API_URL")
    tf_token: SecretStr = Field(..., env="TF_TOKEN")
    gh_token: SecretStr = Field(..., env="GH_TOKEN")
    github_repository: str = Field(..., env="GITHUB_REPOSITORY")
    github_workflow_file: str = Field(default="ansible-runner.yml", env="GITHUB_WORKFLOW_FILE")
    github_ref: str = Field(default="main", env="GITHUB_REF")
    hmac_key: Optional[SecretStr] = Field(None, env="HMAC_KEY")
    github_webhook_secret: Optional[SecretStr] = Field(None, env="GITHUB_WEBHOOK_SECRET")
    redis_url: str = Field(
        default="redis://redis-11952.c56.east-us.azure.redns.redis-cloud.com:11952",
        env="REDIS_URL",
    )

    @field_validator("github_repository")
    def validate_repo_format(cls, v: str) -> str:
        if "/" not in v:
            raise ValueError(f"Invalid GITHUB_REPOSITORY format: {v}")
        return v


@lru_cache()
def get_settings() -> Settings:
    """
    Returns a singleton Settings instance,
    cached so reading env-vars only happens once.
    """
    return Settings()
