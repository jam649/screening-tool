"""Prepare a private adapter once; bridge independent results into immutable Hits."""

from app.adjudicators.budget import InputBudgetError, prepare_state
from app.adjudicators.context import build_state
from app.adjudicators.contracts import Evaluation, ModelConfig
from app.adjudicators.http import HTTPAdjudicator


def evaluate(
    config: ModelConfig, party: dict, entry: dict, explanation: dict, *, transport=None
) -> Evaluation:
    try:
        state, budget = prepare_state(
            build_state(party, entry, explanation), explanation, config.provider
        )
        return HTTPAdjudicator(config, transport=transport).evaluate(state, budget=budget)
    except InputBudgetError as exc:
        return Evaluation(
            provider=config.provider,
            requested_model=config.requested_model,
            error_code="input_budget_exceeded",
            budget=exc.metadata,
        )
    except Exception:
        # Never include exception text, request fields, endpoint or key values.
        return Evaluation(
            provider=config.provider,
            requested_model=config.requested_model,
            error_code="adapter_unavailable",
        )


def build_adjudication_hook(db):
    from app.adjudicators.settings import load_model_config
    from app.screening.engine import Adjudication

    settings = db.info.get("config")
    if settings is None:
        return ModelConfig(), None
    model = load_model_config(db, settings)
    if model.provider == "none":
        return model, None
    loader = db.info.get("model_transport_loader")
    transport = loader() if loader else db.info.get("model_transport")

    def hook(party, entry, score, explanation, screening_settings):
        result = evaluate(model, party, entry, explanation, transport=transport)
        metadata = result.metadata()
        fallback = None
        if result.error_code:
            fallback = {
                "provider": model.provider,
                "requested_model": model.requested_model,
                "error_code": result.error_code,
                "party_id": party.get("id"),
                "csl_entry_id": entry.get("id"),
                "attempts": result.attempts,
            }
        return Adjudication(
            model_prob=result.model_prob,
            model_name=result.returned_model or model.requested_model,
            fallback=fallback,
            metadata=metadata,
        )

    return model, hook
