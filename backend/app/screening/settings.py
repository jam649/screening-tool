"""Validated run configuration, frozen before any comparison is inserted."""

from dataclasses import asdict, dataclass, field
from math import ceil
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import PUBLIC_SETTING_KEYS, redact_credentials
from app.models import AuditLog, Setting
from app.screening.normalize import DEFAULT_LEGAL_SUFFIXES

DEFAULT_STOPWORDS = (
    "the",
    "of",
    "and",
    "company",
    "international",
    "trading",
    "group",
    "general",
    "global",
    "services",
)
DEFAULT_WEIGHTS = {
    "token_set": 0.5,
    "token_sort": 0.25,
    "partial": 0.25,
    "country_match": 5.0,
    "country_mismatch": -15.0,
    "city_match": 5.0,
    "address_max": 5.0,
    "entity_mismatch": -10.0,
    "short_name": -10.0,
}
AUTO_CLEAR_NOTICE = (
    "Future review items scoring below the chosen value will be recorded without human review. "
    "Existing review items remain until screened again."
)
AUTO_CLEAR_ACK_KEY = "auto_clear_acknowledgment_audit_id"
AUTO_CLEAR_KEYS = frozenset({"auto_clear_enabled", "auto_clear_threshold", AUTO_CLEAR_ACK_KEY})


class ScreeningValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ScreeningSettings:
    candidate_min: float = 50
    review_threshold: float = 65
    likely_threshold: float = 90
    score_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    legal_suffixes: tuple[str, ...] = DEFAULT_LEGAL_SUFFIXES
    candidate_stopwords: tuple[str, ...] = DEFAULT_STOPWORDS
    enabled_source_lists: tuple[str, ...] | None = None
    auto_clear_enabled: bool = False
    auto_clear_threshold: int = 65
    auto_clear_acknowledgment_audit_id: str | None = None

    def __post_init__(self):
        import math

        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            for value in (self.candidate_min, self.review_threshold, self.likely_threshold)
        ):
            raise ScreeningValidationError("Thresholds must be finite numbers")
        if not 0 <= self.candidate_min <= self.review_threshold <= self.likely_threshold <= 100:
            raise ScreeningValidationError(
                "Thresholds must satisfy 0 <= candidate <= review <= likely <= 100"
            )
        if not isinstance(self.auto_clear_enabled, bool):
            raise ScreeningValidationError("Auto-clear enabled must be a boolean")
        if isinstance(self.auto_clear_threshold, bool) or not isinstance(
            self.auto_clear_threshold, int
        ):
            raise ScreeningValidationError("Auto-clear threshold must be an integer")
        if not self.review_threshold <= self.auto_clear_threshold <= self.likely_threshold:
            raise ScreeningValidationError(
                "Auto-clear threshold must be between review and likely thresholds"
            )
        if self.auto_clear_acknowledgment_audit_id is not None:
            try:
                UUID(self.auto_clear_acknowledgment_audit_id)
            except (ValueError, TypeError, AttributeError):
                raise ScreeningValidationError(
                    "Auto-clear acknowledgment reference is invalid"
                ) from None
        if set(self.score_weights) != set(DEFAULT_WEIGHTS) or any(
            not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v)
            for v in self.score_weights.values()
        ):
            raise ScreeningValidationError(
                "Score weights must contain finite values for every component"
            )
        if (
            any(self.score_weights[key] < 0 for key in ("token_set", "token_sort", "partial"))
            or abs(
                sum(self.score_weights[key] for key in ("token_set", "token_sort", "partial")) - 1
            )
            > 1e-9
        ):
            raise ScreeningValidationError(
                "The three name weights must be nonnegative and sum to one"
            )
        for key in ("legal_suffixes", "candidate_stopwords", "enabled_source_lists"):
            value = getattr(self, key)
            if value is None and key != "enabled_source_lists":
                raise ScreeningValidationError(f"{key} must be a list of strings")
            if value is not None and (
                not isinstance(value, (tuple, list))
                or any(not isinstance(item, str) or not item for item in value)
            ):
                raise ScreeningValidationError(f"{key} must contain nonempty strings")

    def snapshot(self) -> dict[str, Any]:
        return redact_credentials(asdict(self))

    def scoring_snapshot(self) -> dict[str, Any]:
        return {key: value for key, value in self.snapshot().items() if key not in AUTO_CLEAR_KEYS}

    def auto_clear_snapshot(self) -> dict[str, Any]:
        return {key: value for key, value in self.snapshot().items() if key in AUTO_CLEAR_KEYS}


SCORING_SETTING_KEYS = frozenset(ScreeningSettings.__dataclass_fields__) - AUTO_CLEAR_KEYS


def acknowledgment_valid(db: Session, settings: ScreeningSettings) -> bool:
    reference = settings.auto_clear_acknowledgment_audit_id
    event = db.get(AuditLog, reference) if reference else None
    if (
        event is None
        or event.action != "settings.auto_clear_acknowledged"
        or event.actor_user_id is None
    ):
        return False
    proof = event.after or {}
    if not isinstance(proof, dict) or not isinstance(proof.get("actor"), dict):
        return False
    actor = proof["actor"]
    return (
        proof.get("acknowledgment") == "I understand"
        and proof.get("enabled") is True
        and proof.get("notice") == AUTO_CLEAR_NOTICE
        and actor.get("id") == event.actor_user_id
        and actor.get("role") == "admin"
        and actor.get("active") is True
    )


def load_settings(db: Session, *, validate_acknowledgment: bool = True) -> ScreeningSettings:
    fields = set(ScreeningSettings.__dataclass_fields__)
    values = {
        setting.key: setting.value
        for setting in db.scalars(select(Setting))
        if setting.key in fields
        and (setting.key in PUBLIC_SETTING_KEYS or setting.key == AUTO_CLEAR_ACK_KEY)
        and not setting.encrypted
    }
    if "score_weights" in values:
        if not isinstance(values["score_weights"], dict):
            raise ScreeningValidationError("Score weights must be an object")
        values["score_weights"] = {**DEFAULT_WEIGHTS, **values["score_weights"]}
    if "auto_clear_threshold" not in values:
        review = values.get("review_threshold", 65)
        if isinstance(review, (int, float)) and not isinstance(review, bool):
            import math

            if math.isfinite(review):
                values["auto_clear_threshold"] = ceil(review)
    settings = ScreeningSettings(**values)
    if (
        settings.auto_clear_enabled
        and validate_acknowledgment
        and not acknowledgment_valid(db, settings)
    ):
        raise ScreeningValidationError("Auto-clear requires a recorded Admin acknowledgment")
    return settings
