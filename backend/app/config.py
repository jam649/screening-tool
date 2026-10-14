from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PRODUCT_NAME = "screening-tool"
DISCLAIMER_URL = "https://developer.trade.gov/terms-of-service"
# Verbatim public-domain ITA Consolidated Screening List Scoring terms, verified 2026-10-13.
DISCLAIMER = (
    "The International Trade Administration does not provide endorsements or recommendations "
    "with regard to any specific CSL search result or score. The CSL is one tool offered to "
    "assist users in identifying potentially sanctioned companies, entities, or persons and "
    "is not a substitute for undertaking appropriate due diligence. In the event that a "
    "company, entity, or person is identified by a CSL search result or score, users should "
    "conduct additional due diligence and make their own risk-based determinations before "
    "proceeding. Use of the CSL does not limit any criminal or civil liability for any act "
    "undertaken as a result of, or in reliance on, such use."
)


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCREENING_TOOL_", extra="ignore", hide_input_in_errors=True
    )
    secret_key: SecretStr
    database_url: str = "sqlite:///./data/screening.db"
    frontend_dist: Path = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    session_hours: int = Field(default=12, ge=1)
    login_attempt_limit: int = Field(default=5, ge=1)
    login_window_seconds: int = Field(default=900, ge=1)
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "::1"]
    allowed_origins: list[str] = []

    @field_validator("allowed_hosts")
    @classmethod
    def validate_hosts(cls, values: list[str]) -> list[str]:
        if not values or any(
            not value or "*" in value or "/" in value or "://" in value for value in values
        ):
            raise ValueError("Configure explicit trusted host names without wildcards")
        return values

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, values: list[str]) -> list[str]:
        for value in values:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Configure origins as exact http(s) scheme and host pairs")
        return values

    @field_validator("secret_key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        try:
            Fernet(value.get_secret_value().encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("SCREENING_TOOL_SECRET_KEY must be a Fernet key") from exc
        return value

    @field_validator("database_url")
    @classmethod
    def sqlite_only(cls, value: str) -> str:
        if not value.startswith("sqlite:"):
            raise ValueError("This release requires SQLite")
        return value
