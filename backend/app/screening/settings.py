"""Validated run configuration, frozen before any comparison is inserted."""

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import PUBLIC_SETTING_KEYS, redact_credentials
from app.models import Setting
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


def load_settings(db: Session) -> ScreeningSettings:
    fields = set(ScreeningSettings.__dataclass_fields__)
    values = {
        setting.key: setting.value
        for setting in db.scalars(select(Setting))
        if setting.key in fields and setting.key in PUBLIC_SETTING_KEYS and not setting.encrypted
    }
    if "score_weights" in values:
        if not isinstance(values["score_weights"], dict):
            raise ScreeningValidationError("Score weights must be an object")
        values["score_weights"] = {**DEFAULT_WEIGHTS, **values["score_weights"]}
    return ScreeningSettings(**values)
