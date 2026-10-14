"""Admin-only model settings and bounded tests of the saved provider."""

from dataclasses import replace
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictFloat, StrictInt

from app.adjudicators.contracts import PRIVACY_NOTICE, ModelConfigurationError
from app.adjudicators.settings import (
    ACK_ACTION,
    load_model_config,
    model_acknowledgment_valid,
    public_model_settings,
    save_model_config,
    validate_laya_base_url,
)
from app.api.deps import DB, AdminUser, AuthSession
from app.api.review import begin_write
from app.audit import audit
from app.csl.service import current_version
from app.models import ScreeningTask, User, UserSession, utcnow

router = APIRouter()


class Generation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_generation: StrictInt = Field(ge=0)


class Changes(Generation):
    provider: Literal["none", "jev", "laya"] | None = None
    w_det: StrictFloat | StrictInt | None = None
    w_model: StrictFloat | StrictInt | None = None
    laya_base_url: str | None = Field(default=None, max_length=2048)
    jev_api_key: SecretStr | None = Field(default=None, min_length=1, max_length=8192)
    laya_api_key: SecretStr | None = Field(default=None, min_length=1, max_length=8192)
    clear_jev_api_key: bool = False
    clear_laya_api_key: bool = False
    hosted_acknowledgment: str | None = Field(default=None, max_length=1000)


def active_admin_session(db, actor_id, session_id):
    actor = db.get(User, actor_id, populate_existing=True)
    session = db.get(UserSession, session_id, populate_existing=True)
    if (
        actor is None
        or not actor.active
        or actor.role != "admin"
        or session is None
        or session.user_id != actor_id
        or session.expires_at <= utcnow()
    ):
        raise HTTPException(403, "Active Admin session required")
    return actor


def read_model(db, config):
    try:
        return load_model_config(db, config)
    except ModelConfigurationError:
        raise HTTPException(503, "Model configuration unavailable") from None


@router.get("/settings/model")
def get_model(request: Request, user: AdminUser, db: DB):
    read_model(db, request.app.state.config)
    return public_model_settings(db, request.app.state.config)


@router.patch("/settings/model")
def patch_model(body: Changes, request: Request, user: AdminUser, session: AuthSession, db: DB):
    actor_id, session_id = user.id, session.id
    config = request.app.state.config
    begin_write(db)
    actor = active_admin_session(db, actor_id, session_id)
    old = read_model(db, config)
    if body.expected_generation != old.generation:
        raise HTTPException(409, "Model settings changed; refresh and retry")
    values = {
        field: getattr(body, field)
        for field in ("provider", "w_det", "w_model", "laya_base_url")
        if getattr(body, field) is not None
    }
    try:
        if "laya_base_url" in values:
            values["laya_base_url"] = validate_laya_base_url(values["laya_base_url"])
        for key_name in ("jev_api_key", "laya_api_key"):
            entered = getattr(body, key_name)
            clear = getattr(body, "clear_" + key_name)
            if entered is not None and clear:
                raise ModelConfigurationError("Choose replacement key or clear")
            if entered is not None:
                key = entered.get_secret_value()
                if any(not 32 <= ord(c) <= 126 for c in key):
                    raise ModelConfigurationError("Key must use printable ASCII characters")
                values[key_name] = key
            elif clear:
                values[key_name] = ""
        if body.jev_api_key is not None:
            if body.hosted_acknowledgment != PRIVACY_NOTICE:
                raise ModelConfigurationError("Acknowledge the displayed hosted-data notice")
        elif body.hosted_acknowledgment is not None:
            raise ModelConfigurationError("Acknowledgment is only accepted with a Jev key")
        updated = replace(old, **values)
        validate_laya_base_url(updated.laya_base_url)
        if updated.provider == "jev" and (
            not updated.jev_api_key
            or (body.jev_api_key is None and not model_acknowledgment_valid(db, updated))
        ):
            raise ModelConfigurationError("Save an acknowledged Jev key before enabling Jev")
    except ModelConfigurationError as error:
        raise HTTPException(422, str(error)) from None
    before = public_model_settings(db, config)
    # A repeated acknowledged key does not create another policy version or audit proof.
    task = None
    if updated != old:
        if body.jev_api_key is not None:
            proof = audit(
                db,
                ACK_ACTION,
                "settings",
                "model_config",
                after={
                    "notice": PRIVACY_NOTICE,
                    "acknowledgment": PRIVACY_NOTICE,
                    "actor": {
                        "id": actor.id,
                        "email": actor.email,
                        "display_name": actor.display_name,
                        "role": actor.role,
                        "active": actor.active,
                    },
                },
                actor_user_id=actor_id,
            )
            db.flush()
            updated = replace(updated, hosted_acknowledgment_audit_id=proof.id)
        updated = replace(updated, generation=old.generation + 1)
        save_model_config(db, config, updated)
        version = current_version(db)
        if version is not None:
            task = ScreeningTask(
                trigger="manual", csl_version_id=version.id, party_ids=[], entry_ids=[]
            )
            db.add(task)
            db.flush()
        audit(
            db,
            "settings.model_changed",
            "settings",
            "model_config",
            before=before,
            after={
                **public_model_settings(db, config),
                "full_screening_task_id": task.id if task else None,
            },
            actor_user_id=actor_id,
        )
    task_id = task.id if task else None
    result = public_model_settings(db, config)
    db.commit()
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None and task_id:
        scheduler.wake()
    return {**result, "pending_screening_task_id": task_id}


@router.post("/settings/model/test")
def test_model(body: Generation, request: Request, user: AdminUser, session: AuthSession, db: DB):
    from app.adjudicators.runner import evaluate

    actor_id, session_id = user.id, session.id
    config = request.app.state.config
    model = read_model(db, config)
    if model.generation != body.expected_generation:
        raise HTTPException(409, "Model settings changed; refresh and retry")
    if model.provider == "none":
        raise HTTPException(422, "Select and save a model provider first")
    # Release authentication/settings reads before provider HTTP. No customer data accepted.
    db.rollback()
    loader = db.info.get("model_transport_loader")
    transport = loader() if loader else getattr(request.app.state, "model_transport", None)
    evaluation = evaluate(
        model,
        {"name": "Synthetic Test Company", "country_iso2": "US", "party_type": "entity"},
        {"name": "Synthetic Test Company", "country_iso2": "US", "entity_type": "entity"},
        {"synthetic_connection_test": True},
        transport=transport,
    )
    try:
        begin_write(db)
        active_admin_session(db, actor_id, session_id)
        current = read_model(db, config)
        if current.generation != model.generation:
            raise HTTPException(409, "Model settings changed; refresh and retry")
        result = {
            "status": "failed"
            if evaluation.error_code or evaluation.model_prob is None
            else "connected",
            "provider": model.provider,
            "generation": model.generation,
            "requested_model": evaluation.requested_model,
            "returned_model": evaluation.returned_model,
            "model_prob": evaluation.model_prob,
            "usage": {
                "input_tokens": evaluation.input_tokens,
                "output_tokens": evaluation.output_tokens,
            },
            "attempts": evaluation.attempts,
            "elapsed_ms": evaluation.elapsed_ms,
            "error_code": evaluation.error_code,
        }
        audit(
            db,
            "settings.model_tested",
            "settings",
            "model_config",
            after=result,
            actor_user_id=actor_id,
        )
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise
