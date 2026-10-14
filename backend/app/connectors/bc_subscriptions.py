"""Durable BC subscription intents, renewal, cleanup and authenticated signals."""

import hmac
import json
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import func, literal_column, select

from app.audit import audit
from app.connectors.bc_auth import (
    BCOperationError,
    acquire_source_token,
    bc_source,
    begin,
    check_snapshot,
    require_authority,
)
from app.connectors.bc_common import (
    API_ORIGIN,
    MAX_CLEANUP,
    RESOURCES,
    WEBHOOK_PATH,
    BCConfig,
    BCConfigurationError,
    canonical_uuid,
    load_app_config,
    load_bc_config,
    load_source_credentials,
    pending_cleanup,
    private_subscriptions,
    public_origin,
    save_source_credentials,
    utc_time,
)
from app.models import AuditLog, User, utcnow
from app.security import digest


def resource_path(company, entity):
    return f"/api/v2.0/companies({canonical_uuid(company)})/{entity}"


def normalize_resource(value, snapshot):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise BCOperationError("invalid_notification")
    prefix = f"{API_ORIGIN}/v2.0/{snapshot.tenant_id}/{snapshot.environment}/"
    if value.startswith(prefix):
        value = value[len(prefix) :]
    elif "://" in value:
        raise BCOperationError("invalid_notification")
    path, _, query = value.partition("?")
    pattern = (
        r"/?api/v2\.0/companies\(([0-9a-fA-F-]{36})\)/(customers|vendors)"
        r"(?:\(([0-9a-fA-F-]{36})\))?"
    )
    match = re.fullmatch(pattern, path)
    if not match or canonical_uuid(match[1]) != snapshot.company_id:
        raise BCOperationError("invalid_notification")
    if match[3]:
        canonical_uuid(match[3])
    return match[2], match[3], query


def trusted_list(client, token):
    rows, links, next_link = [], set(), None
    for _ in range(1000):
        response = client.subscription_request("GET", token, next_link=next_link)
        values = response.body.get("value") if isinstance(response.body, dict) else None
        if not isinstance(values, list) or any(not isinstance(row, dict) for row in values):
            raise BCOperationError("invalid_subscription_response")
        rows.extend(values)
        if len(rows) > 20000:
            raise BCOperationError("invalid_subscription_response")
        next_link = response.body.get("@odata.nextLink")
        if not next_link:
            return rows
        if not isinstance(next_link, str) or next_link in links:
            raise BCOperationError("invalid_subscription_response")
        links.add(next_link)
    raise BCOperationError("invalid_subscription_response")


def matches(rows, obligation):
    result = []
    for row in rows:
        state = row.get("clientState")
        if (
            isinstance(state, str)
            and hmac.compare_digest(state.encode(), obligation["client_state"].encode())
            and row.get("notificationUrl") == obligation["notification_url"]
            and isinstance(row.get("resource"), str)
            and row["resource"].lstrip("/") == obligation["resource_path"].lstrip("/")
        ):
            subscription_id = row.get("subscriptionId")
            if not isinstance(subscription_id, str) or not 1 <= len(subscription_id) <= 1024:
                raise BCOperationError("invalid_subscription_response")
            result.append(row)
    return result


def subscription_from(row, obligation, *, etag=None):
    expiry = row.get("expirationDateTime")
    utc_time(expiry)
    chosen_etag = etag or row.get("@odata.etag")
    if (
        not isinstance(chosen_etag, str)
        or not chosen_etag
        or any(ord(c) < 32 or ord(c) == 127 for c in chosen_etag)
    ):
        raise BCOperationError("subscription_etag_missing")
    return {
        "resource": obligation["resource"],
        "subscription_id": row["subscriptionId"],
        "resource_path": obligation["resource_path"],
        "notification_url": obligation["notification_url"],
        "client_state": obligation["client_state"],
        "etag": chosen_etag,
        "expiration_date_time": expiry,
        "last_error_code": None,
    }


def renew_subscription(client, token, current):
    for attempt in range(2):
        try:
            response = client.subscription_request(
                "PATCH",
                token,
                subscription_id=current["subscription_id"],
                etag=current["etag"],
                payload={
                    "notificationUrl": current["notification_url"],
                    "resource": current["resource_path"],
                    "clientState": current["client_state"],
                },
            )
            if (
                response.status_code != 200
                or not isinstance(response.body, dict)
                or len(matches([response.body], current)) != 1
                or response.body["subscriptionId"] != current["subscription_id"]
            ):
                raise BCOperationError("invalid_subscription_response")
            return subscription_from(response.body, current, etag=response.etag)
        except Exception as error:
            if attempt or getattr(error, "status_code", None) not in {409, 412}:
                raise
            candidates = matches(trusted_list(client, token), current)
            refreshed = next(
                (row for row in candidates if row["subscriptionId"] == current["subscription_id"]),
                None,
            )
            if refreshed is None:
                raise
            current = subscription_from(refreshed, current)
    raise BCOperationError("subscription_renewal_failed")


def cleanup_obligation(snapshot, token, subscription, *, actor=None, purpose="cleanup"):
    return {
        **subscription,
        "tenant_id": snapshot.tenant_id,
        "environment": snapshot.environment,
        "client_id": snapshot.client_id,
        "client_secret": snapshot.client_secret,
        "app_generation": snapshot.app_generation,
        "source_generation": snapshot.source_generation,
        "company_id": snapshot.company_id,
        "access_token": token,
        "issuer_user_id": actor,
        "purpose": purpose,
    }


def save_state(
    db,
    config,
    source_id,
    snapshot,
    *,
    subscriptions=None,
    obligations=None,
    action=None,
    after=None,
    activate_obligation=None,
):
    begin(db)
    source = bc_source(db, source_id)
    check_snapshot(db, config, source, snapshot, authority=False)
    original_actor = db.info.get("actor_user_id")
    if activate_obligation is not None and not may_activate(
        db, config, source, snapshot, activate_obligation
    ):
        from app.scheduler.service import enqueue_bc_subscriptions

        db.info["actor_user_id"] = None
        try:
            values = load_source_credentials(source, config)
            retained = [
                {**row, "purpose": "cleanup"}
                if row["client_state"] == activate_obligation["client_state"]
                else row
                for row in pending_cleanup(values)
            ]
            values["pending_cleanup"] = retained
            save_source_credentials(db, source, config, values)
            source.config = {**source.config, "cleanup_status": "pending"}
            enqueue_bc_subscriptions(db, source_id, actor_user_id=None)
            audit(
                db,
                "bc.subscription_abandoned",
                "sources",
                source_id,
                after={"subscription_id": activate_obligation.get("subscription_id")},
            )
            db.commit()
            return False
        except Exception:
            db.rollback()
            raise
        finally:
            db.info["actor_user_id"] = original_actor
    db.info["actor_user_id"] = None
    try:
        values = load_source_credentials(source, config)
        if subscriptions is not None:
            values["subscriptions"] = subscriptions
        if obligations is not None:
            values["pending_cleanup"] = obligations
        save_source_credentials(db, source, config, values)
        if obligations:
            source.config = {**source.config, "cleanup_status": "pending"}
        if action:
            audit(db, action, "sources", source_id, after=after)
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.info["actor_user_id"] = original_actor


def may_activate(db, config, source, snapshot, obligation):
    if (
        obligation.get("purpose") != "activate"
        or source.config.get("connection_status") != "connected"
        or not source.config.get("webhooks_enabled")
        or obligation["notification_url"] != public_origin(db) + WEBHOOK_PATH + source.id
        or obligation.get("source_generation") != snapshot.source_generation
        or obligation.get("app_generation") != snapshot.app_generation
    ):
        return False
    issuer = obligation.get("issuer_user_id")
    if issuer:
        user = db.get(User, issuer, populate_existing=True)
        if user is None or not user.active or user.role != "admin":
            return False
    try:
        require_authority(db)
    except BCOperationError:
        return False
    return True


def consume_obligations(db, config, source_id, snapshot, *, transport=None, errors=None):
    from app.connectors.bc import BCClient, BCError

    source = bc_source(db, source_id)
    obligations = [dict(row) for row in pending_cleanup(load_source_credentials(source, config))]
    deleted = 0
    for obligation in list(obligations):
        app = load_app_config(db, config)
        captured = BCConfig(
            obligation["tenant_id"],
            obligation["environment"],
            obligation.get("company_id"),
            obligation["client_id"],
            app.client_secret
            if app.client_id == obligation["client_id"]
            else obligation["client_secret"],
            snapshot.app_generation,
            snapshot.source_generation,
        )
        client = BCClient(captured, transport=transport)
        db.rollback()
        try:
            token = client.acquire_token().get("access_token")
            remote = trusted_list(client, token)
            rows = matches(remote, obligation)
            known_id = obligation.get("subscription_id")
            if not rows and known_id:
                if any(row.get("subscriptionId") == known_id for row in remote):
                    raise BCOperationError("subscription_ownership_changed")
                expiry = obligation.get("expiration_date_time")
                expires = (
                    utc_time(expiry)
                    if expiry
                    else (
                        utc_time(obligation["accepted_at"]) + timedelta(days=3)
                        if obligation.get("accepted_at")
                        else None
                    )
                )
                if expires and expires + timedelta(minutes=5) <= utcnow():
                    obligations = [row for row in obligations if row is not obligation]
                    save_state(
                        db,
                        config,
                        source_id,
                        snapshot,
                        obligations=obligations,
                        action="bc.subscription_expired",
                        after={"subscription_id": known_id},
                    )
                    continue
                # Exact accepted identity survives eventual list visibility.
                # With its actual ETag we can adopt or delete it safely; without
                # one it stays private until visible or proven expired/absent.
                if not obligation.get("etag") or not expiry:
                    raise BCOperationError("subscription_visibility_pending")
                rows = [
                    {
                        "subscriptionId": known_id,
                        "expirationDateTime": expiry,
                        "@odata.etag": obligation["etag"],
                    }
                ]
            if not rows and not known_id and obligation.get("requested_at"):
                if utc_time(obligation["requested_at"]) + timedelta(days=3, minutes=5) > utcnow():
                    raise BCOperationError("subscription_visibility_pending")
            source = bc_source(db, source_id)
            activate = may_activate(db, config, source, snapshot, obligation)
            db.rollback()
            if activate and len(rows) == 1:
                current = subscription_from(rows[0], obligation)
                if utc_time(current["expiration_date_time"]) <= utcnow():
                    activate = False
                else:
                    values = load_source_credentials(bc_source(db, source_id), config)
                    subscriptions = [
                        row
                        for row in private_subscriptions(values)
                        if row["resource"] != current["resource"]
                    ]
                    remaining = [row for row in obligations if row is not obligation]
                    adopted = save_state(
                        db,
                        config,
                        source_id,
                        snapshot,
                        subscriptions=[*subscriptions, current],
                        obligations=remaining,
                        activate_obligation=obligation,
                        action="bc.subscription_registered",
                        after={
                            "subscription_id": current["subscription_id"],
                            "resource": current["resource"],
                            "expiration_date_time": current["expiration_date_time"],
                        },
                    )
                    if adopted:
                        obligations = remaining
                    continue
            for row in rows:
                etag = row.get("@odata.etag")
                if not etag:
                    raise BCOperationError("subscription_etag_missing")
                try:
                    client.subscription_request(
                        "DELETE", token, subscription_id=row["subscriptionId"], etag=etag
                    )
                except Exception as error:
                    if getattr(error, "status_code", None) not in {404, 409, 412}:
                        raise
                    refreshed = matches(trusted_list(client, token), obligation)
                    current = next(
                        (
                            item
                            for item in refreshed
                            if item["subscriptionId"] == row["subscriptionId"]
                        ),
                        None,
                    )
                    if current:
                        client.subscription_request(
                            "DELETE",
                            token,
                            subscription_id=current["subscriptionId"],
                            etag=current.get("@odata.etag"),
                        )
                deleted += 1
            obligations = [row for row in obligations if row is not obligation]
            save_state(
                db,
                config,
                source_id,
                snapshot,
                obligations=obligations,
                action="bc.subscription_cleanup",
                after={"removed": len(rows)},
            )
        except (BCError, BCOperationError, BCConfigurationError) as error:
            if errors is None or getattr(error, "code", None) in {
                "connection_changed",
                "admin_required",
            }:
                raise
            errors.append("bc_subscription_failed")
            db.rollback()
        finally:
            client.close()
    return deleted


def move_registered_to_cleanup(db, config, source_id, snapshot):
    source = bc_source(db, source_id)
    values = load_source_credentials(source, config)
    obligations = pending_cleanup(values)
    registered = private_subscriptions(values)
    available = MAX_CLEANUP - len(obligations)
    moving, retained = registered[:available], registered[available:]
    if moving:
        save_state(
            db,
            config,
            source_id,
            snapshot,
            subscriptions=retained,
            obligations=[
                *obligations,
                *(cleanup_obligation(snapshot, values.get("access_token"), row) for row in moving),
            ],
        )
    return bool(moving), len(retained)


def _reconcile(db, config, source, *, transport=None, disconnect=False):
    from app.connectors.bc import BCClient

    source_id = source.id
    errors: list[str] = []
    snapshot = load_bc_config(db, config, source)
    desired = (
        source.config.get("connection_status") == "connected"
        and source.config.get("webhooks_enabled")
        and not disconnect
    )
    # Drain earlier custody before trying to add current subscriptions. Capacity
    # fallback from the sync worker may legitimately retain both namespaces.
    removed = consume_obligations(
        db, config, source_id, snapshot, transport=transport, errors=errors
    )
    if not desired:
        moved, retained = move_registered_to_cleanup(db, config, source_id, snapshot)
        if moved:
            removed += consume_obligations(
                db, config, source_id, snapshot, transport=transport, errors=errors
            )
        if retained:
            errors.append("bc_cleanup_pending")
    source = bc_source(db, source_id)
    if desired:
        snapshot, token = acquire_source_token(db, config, source, transport=transport)
        origin = public_origin(db)
        if urlsplit(origin).scheme != "https" or not urlsplit(origin).hostname:
            raise BCOperationError("public_https_required")
        db.rollback()
        client = BCClient(snapshot, transport=transport)
        try:
            remote = trusted_list(client, token)
            for entity in RESOURCES:
                source = bc_source(db, source_id)
                values = load_source_credentials(source, config)
                existing = next(
                    (row for row in private_subscriptions(values) if row["resource"] == entity),
                    None,
                )
                unresolved = any(row.get("resource") == entity for row in pending_cleanup(values))
                db.rollback()
                if unresolved:
                    continue
                if existing and existing["notification_url"] != origin + WEBHOOK_PATH + source_id:
                    latest = load_source_credentials(bc_source(db, source_id), config)
                    save_state(
                        db,
                        config,
                        source_id,
                        snapshot,
                        subscriptions=[
                            row
                            for row in private_subscriptions(latest)
                            if row["resource"] != entity
                        ],
                        obligations=[
                            *pending_cleanup(latest),
                            cleanup_obligation(snapshot, token, existing),
                        ],
                    )
                    removed += consume_obligations(
                        db, config, source_id, snapshot, transport=transport, errors=errors
                    )
                    latest = load_source_credentials(bc_source(db, source_id), config)
                    db.rollback()
                    if any(row.get("resource") == entity for row in pending_cleanup(latest)):
                        continue
                    existing = None
                if existing:
                    candidates = matches(remote, existing)
                    if len(candidates) == 1:
                        try:
                            current = subscription_from(candidates[0], existing)
                            if utc_time(current["expiration_date_time"]) <= utcnow():
                                raise BCOperationError("subscription_expired")
                            if utc_time(current["expiration_date_time"]) > utcnow() + timedelta(
                                hours=36
                            ):
                                if current != existing:
                                    latest = load_source_credentials(
                                        bc_source(db, source_id), config
                                    )
                                    save_state(
                                        db,
                                        config,
                                        source_id,
                                        snapshot,
                                        subscriptions=[
                                            row
                                            for row in private_subscriptions(latest)
                                            if row["resource"] != entity
                                        ]
                                        + [current],
                                        action="bc.subscription_observed",
                                        after={
                                            "subscription_id": current["subscription_id"],
                                            "expiration_date_time": current["expiration_date_time"],
                                        },
                                    )
                                continue
                            updated = renew_subscription(client, token, current)
                            latest = load_source_credentials(bc_source(db, source_id), config)
                            save_state(
                                db,
                                config,
                                source_id,
                                snapshot,
                                subscriptions=[
                                    row
                                    for row in private_subscriptions(latest)
                                    if row["resource"] != entity
                                ]
                                + [updated],
                                action="bc.subscription_renewed",
                                after={
                                    "subscription_id": updated["subscription_id"],
                                    "expiration_date_time": updated["expiration_date_time"],
                                },
                            )
                            continue
                        except Exception as error:
                            if getattr(error, "status_code", None) not in {
                                404,
                                409,
                                412,
                            } and not isinstance(error, BCOperationError):
                                raise
                            # A missing/expired/conflicted subscription must be
                            # reconciled and cleaned before a new POST.
                            obligation = cleanup_obligation(snapshot, token, existing)
                            latest = load_source_credentials(bc_source(db, source_id), config)
                            save_state(
                                db,
                                config,
                                source_id,
                                snapshot,
                                subscriptions=[
                                    row
                                    for row in private_subscriptions(latest)
                                    if row["resource"] != entity
                                ],
                                obligations=[*pending_cleanup(latest), obligation],
                            )
                            removed += consume_obligations(
                                db, config, source_id, snapshot, transport=transport, errors=errors
                            )
                    else:
                        latest = load_source_credentials(bc_source(db, source_id), config)
                        save_state(
                            db,
                            config,
                            source_id,
                            snapshot,
                            subscriptions=[
                                row
                                for row in private_subscriptions(latest)
                                if row["resource"] != entity
                            ],
                            obligations=[
                                *pending_cleanup(latest),
                                cleanup_obligation(snapshot, token, existing),
                            ],
                        )
                        removed += consume_obligations(
                            db, config, source_id, snapshot, transport=transport, errors=errors
                        )
                source = bc_source(db, source_id)
                if any(
                    row.get("resource") == entity
                    for row in pending_cleanup(load_source_credentials(source, config))
                ):
                    db.rollback()
                    continue
                begin(db)
                source = bc_source(db, source_id)
                check_snapshot(db, config, source, snapshot)
                if source.config.get("connection_status") != "connected" or not source.config.get(
                    "webhooks_enabled"
                ):
                    db.rollback()
                    break
                intent = cleanup_obligation(
                    snapshot,
                    token,
                    {
                        "resource": entity,
                        "subscription_id": None,
                        "resource_path": resource_path(snapshot.company_id, entity),
                        "notification_url": origin + WEBHOOK_PATH + source_id,
                        "client_state": secrets.token_urlsafe(32),
                        "requested_at": utcnow().isoformat(),
                    },
                    actor=db.info.get("actor_user_id"),
                    purpose="activate",
                )
                latest = load_source_credentials(source, config)
                db.rollback()
                save_state(
                    db,
                    config,
                    source_id,
                    snapshot,
                    obligations=[*pending_cleanup(latest), intent],
                    action="bc.subscription_requested",
                    after={"resource": entity},
                )
                try:
                    response = client.subscription_request(
                        "POST",
                        token,
                        payload={
                            "notificationUrl": intent["notification_url"],
                            "resource": intent["resource_path"],
                            "clientState": intent["client_state"],
                        },
                    )
                except Exception as error:
                    status = getattr(error, "status_code", None)
                    if isinstance(status, int) and 400 <= status < 500 and status not in {408, 429}:
                        latest = load_source_credentials(bc_source(db, source_id), config)
                        save_state(
                            db,
                            config,
                            source_id,
                            snapshot,
                            obligations=[
                                row
                                for row in pending_cleanup(latest)
                                if row["client_state"] != intent["client_state"]
                            ],
                            action="bc.subscription_rejected",
                            after={"resource": entity, "http_status": status},
                        )
                    else:
                        db.rollback()
                    errors.append("bc_subscription_failed")
                    continue
                accepted = response.body
                if (
                    response.status_code != 201
                    or not isinstance(accepted, dict)
                    or len(matches([accepted], intent)) != 1
                ):
                    raise BCOperationError("invalid_subscription_response")
                utc_time(accepted.get("expirationDateTime"))
                accepted_intent = {
                    **intent,
                    "subscription_id": accepted["subscriptionId"],
                    "expiration_date_time": accepted["expirationDateTime"],
                    "etag": response.etag or accepted.get("@odata.etag"),
                    "accepted_at": utcnow().isoformat(),
                }
                latest = load_source_credentials(bc_source(db, source_id), config)
                save_state(
                    db,
                    config,
                    source_id,
                    snapshot,
                    obligations=[
                        accepted_intent if row["client_state"] == intent["client_state"] else row
                        for row in pending_cleanup(latest)
                    ],
                    action="bc.subscription_accepted",
                    after={
                        "subscription_id": accepted_intent["subscription_id"],
                        "resource": entity,
                        "expiration_date_time": accepted_intent["expiration_date_time"],
                    },
                )
                # Accepted identity is committed before GET or late authority
                # decisions. Uncertain POSTs retain their pre-request intent.
                removed += consume_obligations(
                    db, config, source_id, snapshot, transport=transport, errors=errors
                )
                remote = trusted_list(client, token)
        finally:
            client.close()
    source = bc_source(db, source_id)
    if source.config.get("connection_status") != "connected" or not source.config.get(
        "webhooks_enabled"
    ):
        moved, retained = move_registered_to_cleanup(db, config, source_id, snapshot)
        if moved:
            removed += consume_obligations(
                db, config, source_id, snapshot, transport=transport, errors=errors
            )
        if retained:
            errors.append("bc_cleanup_pending")
    begin(db)
    source = bc_source(db, source_id)
    check_snapshot(db, config, source, snapshot, authority=False)
    values = load_source_credentials(source, config)
    left, current = pending_cleanup(values), private_subscriptions(values)
    cleanup_required = bool(left) or (
        bool(current)
        and (
            source.config.get("connection_status") != "connected"
            or not source.config.get("webhooks_enabled")
        )
    )
    source.config = {
        **source.config,
        "cleanup_status": "failed"
        if cleanup_required and errors
        else "pending"
        if cleanup_required
        else "completed"
        if removed
        else "not_required",
        "cleanup_error_code": "bc_cleanup_failed" if cleanup_required and errors else None,
        "subscription_error_code": "bc_subscription_failed" if errors else None,
    }
    if disconnect or source.config.get("connection_status") in {"disconnecting", "disconnected"}:
        source.config = {
            **source.config,
            "connection_status": "disconnected",
            "webhooks_enabled": False,
        }
        if not left and not current:
            source.credentials_enc = None
            source.config = {
                **source.config,
                "connection_generation": snapshot.source_generation + 1,
            }
    db.commit()
    return {
        "source_id": source_id,
        "connection_status": source.config["connection_status"],
        "cleanup_status": source.config["cleanup_status"],
        "subscription_status": "degraded"
        if errors
        else "active"
        if len(current) == 2 and desired
        else "disabled",
        "subscription_count": len(current),
        "error_code": "bc_subscription_failed" if errors else None,
    }


def reconcile_bc_subscriptions(db, config, source, *, transport=None):
    source_id, snapshot = source.id, load_bc_config(db, config, source)
    try:
        return _reconcile(db, config, source, transport=transport)
    except Exception:
        db.rollback()
        begin(db)
        source = bc_source(db, source_id)
        check_snapshot(db, config, source, snapshot, authority=False)
        original_actor = db.info.get("actor_user_id")
        db.info["actor_user_id"] = None
        try:
            cleanup = pending_cleanup(load_source_credentials(source, config))
            source.config = {
                **source.config,
                "subscription_error_code": "bc_subscription_failed",
                "cleanup_status": "failed"
                if cleanup
                else source.config.get("cleanup_status", "not_required"),
                "cleanup_error_code": "bc_cleanup_failed" if cleanup else None,
            }
            if source.config.get("connection_status") == "disconnecting":
                # Local disconnect is complete even when remote subscription
                # cleanup fails. Keep its encrypted obligations for retry.
                source.config = {
                    **source.config,
                    "connection_status": "disconnected",
                    "webhooks_enabled": False,
                }
            audit(
                db,
                "bc.subscription_failed",
                "sources",
                source_id,
                after={"pending_cleanup": len(cleanup)},
            )
            db.commit()
        finally:
            db.info["actor_user_id"] = original_actor
        return {
            "source_id": source_id,
            "connection_status": source.config.get("connection_status", "configured"),
            "cleanup_status": source.config["cleanup_status"],
            "subscription_status": "degraded",
            "subscription_count": len(
                private_subscriptions(load_source_credentials(source, config))
            ),
            "error_code": "bc_subscription_failed",
        }


def disconnect_bc_source(db, config, source, *, transport=None):
    return reconcile_bc_subscriptions(db, config, source, transport=transport)


def webhook_signals(db, config, source_id, raw):
    from app.scheduler.service import enqueue_source_poll

    try:
        message = json.loads(raw)
        events = message.get("value")
        if not isinstance(events, list) or not 1 <= len(events) <= 1000:
            raise ValueError
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
        raise BCOperationError("invalid_notification") from None
    # Anonymous inputs authenticate entirely under ordinary reads. Invalid
    # payloads, unknown sources and wrong secrets never take the SQLite writer.
    source = bc_source(db, source_id)
    snapshot = load_bc_config(db, config, source)
    if source.config.get("connection_status") != "connected" or not source.config.get(
        "webhooks_enabled"
    ):
        db.rollback()
        return
    registered = private_subscriptions(load_source_credentials(source, config))

    def authentication_state(rows):
        return sorted(
            (
                row["resource"],
                row["subscription_id"],
                row["client_state"],
                row["resource_path"],
                row["notification_url"],
            )
            for row in rows
        )

    authenticated_state = authentication_state(registered)
    signals = []
    for event in events:
        if not isinstance(event, dict):
            raise BCOperationError("invalid_notification")
        state = event.get("clientState")
        row = next(
            (row for row in registered if row["subscription_id"] == event.get("subscriptionId")),
            None,
        )
        if (
            not row
            or not isinstance(state, str)
            or not hmac.compare_digest(state.encode(), row["client_state"].encode())
        ):
            raise BCOperationError("invalid_notification")
        entity, record_id, query = normalize_resource(event.get("resource"), snapshot)
        change = event.get("changeType")
        if entity != row["resource"] or change not in {
            "created",
            "updated",
            "deleted",
            "collection",
        }:
            raise BCOperationError("invalid_notification")
        if change != "collection" and (not record_id or query):
            raise BCOperationError("invalid_notification")
        modified = event.get("lastModifiedDateTime")
        utc_time(modified)
        key = digest(
            json.dumps(
                [
                    source_id,
                    snapshot.source_generation,
                    row["subscription_id"],
                    event["resource"],
                    change,
                    modified,
                ],
                separators=(",", ":"),
            )
        )
        signals.append(
            (key, "full" if change in {"deleted", "collection"} else "incremental", change)
        )
    begin(db)
    current = bc_source(db, source_id)
    if (
        load_bc_config(db, config, current) != snapshot
        or current.config.get("connection_status") != "connected"
        or not current.config.get("webhooks_enabled")
        or authentication_state(private_subscriptions(load_source_credentials(current, config)))
        != authenticated_state
    ):
        db.rollback()
        # This was authenticated input, but its authority changed before the
        # receipt writer. A retryable response preserves delivery atomicity.
        raise RuntimeError("Business Central receipt state changed; retry")
    try:
        for key, mode, change in signals:
            if db.scalar(
                select(AuditLog.id).where(
                    AuditLog.action == "bc.webhook_received",
                    func.json_extract(AuditLog.after, literal_column("'$.event_key'")) == key,
                )
            ):
                continue
            attempt = enqueue_source_poll(db, source_id, mode=mode)
            audit(
                db,
                "bc.webhook_received",
                "sources",
                source_id,
                after={"event_key": key, "change_type": change, "attempt_id": attempt.id},
            )
            db.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise
