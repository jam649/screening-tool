"""Immutable policy and per-call results for optional identity adjudication."""

from dataclasses import asdict, dataclass, field
from math import isfinite
from typing import Literal

PRIVACY_NOTICE = (
    "Enabling Jev sends your customer and vendor names and addresses to TypeSafe AI "
    "for scoring. Laya keeps all data on this server."
)
CLAIM = "These two records refer to the same party."
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-1.13.0"
LAYA_MODEL = "multilingual"
LAYA_REVISION = "7b928d828b7b0e022f929d9bd2e44165aa270148"
LAYA_TOKENIZER_SHA256 = "609d8f4c067cd3950f88594c5a802616cea245823836ef5848ee4fc40aab5b6f"


class ModelConfigurationError(ValueError):
    """Only fixed public messages belong in this exception."""


@dataclass(frozen=True)
class ModelConfig:
    provider: Literal["none", "jev", "laya"] = "none"
    generation: int = 0
    w_det: float = 1.0
    w_model: float = 0.0
    jev_model: str = JEV_MODEL
    laya_base_url: str = "http://127.0.0.1:8009"
    laya_model: str = LAYA_MODEL
    laya_max_tokens: int = 1024
    jev_api_key: str = field(default="", repr=False)
    laya_api_key: str = field(default="", repr=False)
    hosted_acknowledgment_audit_id: str | None = None

    def __post_init__(self):
        if self.provider not in {"none", "jev", "laya"}:
            raise ModelConfigurationError("Choose none, Jev or Laya")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ModelConfigurationError("Model configuration generation is invalid")
        weights = (self.w_det, self.w_model)
        if (
            any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not isfinite(v)
                or not 0 <= v <= 1
                for v in weights
            )
            or abs(sum(weights) - 1) > 1e-9
        ):
            raise ModelConfigurationError(
                "Model weights must be finite, nonnegative and sum to one"
            )
        if (
            self.jev_model != JEV_MODEL
            or self.laya_model != LAYA_MODEL
            or isinstance(self.laya_max_tokens, bool)
            or self.laya_max_tokens != 1024
        ):
            raise ModelConfigurationError("Use the pinned model and token policy")

    @property
    def requested_model(self) -> str | None:
        return (
            self.jev_model
            if self.provider == "jev"
            else self.laya_model
            if self.provider == "laya"
            else None
        )

    def scoring_snapshot(self) -> dict:
        return {
            "provider": self.provider,
            "generation": self.generation,
            "w_det": self.w_det,
            "w_model": self.w_model,
            "requested_model": self.requested_model,
            "laya_base_url": self.laya_base_url if self.provider == "laya" else None,
            "laya_max_tokens": self.laya_max_tokens if self.provider == "laya" else None,
            "laya_tokenizer_sha256": LAYA_TOKENIZER_SHA256 if self.provider == "laya" else None,
            "question": CLAIM if self.provider != "none" else None,
        }


@dataclass(frozen=True)
class Evaluation:
    provider: str
    requested_model: str | None
    model_prob: float | None = None
    returned_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    attempts: int = 0
    elapsed_ms: int = 0
    error_code: str | None = None
    budget: dict = field(default_factory=dict)

    def metadata(self) -> dict:
        return asdict(self)
