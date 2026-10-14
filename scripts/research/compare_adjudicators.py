#!/usr/bin/env python3
"""Research only: paired retained Laya inputs versus Jev through OpenRouter.

Never import the original benchmark: it performs network calls at import time.
Run with backend/.venv/bin/python. Public reconstruction uses pure scoring/parser
and offline tokenizer helpers, without database or product provider adapters.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import os
import statistics
import sys
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
RETAINED = ROOT / ".workspace/laya-validation/results.json"
RETAINED_SHA = "039b9b5b9360a3e129deb0d011fdc8d5f74a3f875380f0e60e132f07a4166aeb"
INPUT_SHA = "f6e69d38fb8298c0e00011a26257e21e75e15fafa75367f215356d7eb5ace68f"
FIXTURE_HASHES = {
    "fixtures/csl_sample_v1.json": (
        "a3d69363591beae1393e60a2b04edbfde60df440c0074292c2df940d7b9ec3a4"
    ),
    "fixtures/parties.csv": "679766d38401aa3311e438ccc4661ff364568755839bc5c6743fbbacdcce6d04",
    "fixtures/expected/hits.json": (
        "bc91c6dc93dced4cb78e3fd34cb5b89fb81745e63b4458761076e54318b6a8bb"
    ),
}
MODEL = "typesafe/jev-1.13"
RETURNED_MODEL = "typesafe/jev-1.13-20260917"
DECISIONS = "https://openrouter.ai/api/alpha/decisions"
METADATA = "https://openrouter.ai/api/v1/models/typesafe/jev-1.13/endpoints"
QUESTION = {
    "same_party": {
        "type": "noul",
        "instructions": "These two records refer to the same party.",
    }
}
CEILING = Decimal("0.10")
PROMPT_PRICE = Decimal("0.000000042")
MAX_PROMPT = 32000
RESERVATION = PROMPT_PRICE * MAX_PROMPT
MAX_BODY = 1024 * 1024


class SafeFailure(Exception):
    """Only a fixed diagnostic code, never upstream bodies or credential values."""


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SafeFailure("invalid_number")
    if not math.isfinite(value):
        raise SafeFailure("invalid_number")
    return float(value)


def amount(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise SafeFailure("invalid_cost")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise SafeFailure("invalid_cost") from None
    if not result.is_finite() or result < 0:
        raise SafeFailure("invalid_cost")
    return result


def integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SafeFailure("invalid_usage")
    return value


def reconstruct_pairs() -> list[dict[str, Any]]:
    """Reconstruct the original fixed experiment from public fixtures/offline tokenizer."""
    sys.path.insert(0, str(ROOT / "backend"))
    from app.adjudicators.budget import local_state_room, local_tokens
    from app.csl.parser import parse_bulk
    from app.screening.normalize import normalize_country
    from app.screening.score import score_pair
    from app.screening.settings import ScreeningSettings

    entries = parse_bulk((ROOT / "fixtures/csl_sample_v1.json").read_bytes()).entries
    entry_map = {entry.key: entry for entry in entries}
    with (ROOT / "fixtures/parties.csv").open(newline="") as handle:
        parties = list(csv.DictReader(handle))
    party_map = {party["external_id"]: party for party in parties}
    cases = json.loads((ROOT / "fixtures/expected/hits.json").read_text())["cases"]
    work = [
        (
            case["external_id"],
            case["category"],
            case["category"] not in {"common_name_country", "address_only"},
            party_map[case["external_id"]],
            entry_map[(case["source_list"], case["csl_id"])],
        )
        for case in cases
    ]
    work.extend(
        (f"control-{index:02}", "unrelated", False, parties[index], entries[(index * 7 + 29) % 300])
        for index in range(60)
    )
    room = local_state_room(QUESTION["same_party"]["instructions"])
    rows = []
    for identifier, category, label, party, entry in work:
        party_input = SimpleNamespace(
            name=party["name"],
            alt_names=[],
            party_type=party["party_type"],
            country_iso2=normalize_country(party["country"]),
            city=party["city"],
            address_lines=party["address_lines"].splitlines(),
        )
        score = score_pair(party_input, entry, ScreeningSettings())
        state = {
            "party": {
                "name": party["name"],
                "alt_names": [],
                "type": party["party_type"],
                "country": normalize_country(party["country"]),
                "city": party["city"],
                "address_lines": party["address_lines"].splitlines(),
                "normalized_name": score.explanation["normalized"]["compared_party"],
            },
            "listing": {
                "name": entry.name,
                "alt_names": entry.alt_names,
                "type": entry.entity_type,
                "countries": entry.countries,
                "addresses": copy.deepcopy(entry.addresses),
                "remarks": entry.remarks,
                "normalized_name": score.explanation["normalized"]["compared_entry"],
            },
            "components": score.explanation["components"],
        }
        before, steps = local_tokens(state), []
        if before > room:
            state["listing"].pop("remarks", None)
            steps.append("remarks")
        if local_tokens(state) > room:
            state["party"]["address_lines"] = []
            for address in state["listing"]["addresses"]:
                address.pop("address", None)
            steps.append("address_lines")
        if local_tokens(state) > room:
            best = score.explanation["best_aliases"]["entry"]
            state["listing"]["alt_names"] = [best] if best in entry.alt_names else []
            steps.append("extra_aliases")
        after = local_tokens(state)
        if after > room:
            raise SafeFailure("public_corpus_budget_exceeded")
        rows.append(
            {
                "id": identifier,
                "category": category,
                "same_identity": label,
                "det_score": score.score,
                "entry_key": entry.key,
                "budget": {
                    "before": before,
                    "after": after,
                    "room": room,
                    "steps": steps,
                    "sequence_tokens": after + 1024 - room,
                    "runtime_state_tokens_dropped": 0,
                },
                "request": {
                    "state": state,
                    "questions": copy.deepcopy(QUESTION),
                    "model": "multilingual",
                    "max_len": 1024,
                },
            }
        )
    return rows


def load_pairs(path: Path | None = None) -> list[dict[str, Any]]:
    for fixture, expected in FIXTURE_HASHES.items():
        if hashlib.sha256((ROOT / fixture).read_bytes()).hexdigest() != expected:
            raise SafeFailure("public_fixture_digest_changed")
    rows = reconstruct_pairs()
    if (
        digest(
            [{"state": r["request"]["state"], "questions": r["request"]["questions"]} for r in rows]
        )
        != INPUT_SHA
    ):
        raise SafeFailure("public_reconstruction_input_digest_changed")
    if path is not None:
        original = retained_pairs(path)
        for fresh, old in zip(rows, original, strict=True):
            if (
                fresh["id"] != old["id"]
                or fresh["same_identity"] != old["same_identity"]
                or fresh["request"] != old["request"]
            ):
                raise SafeFailure("public_reconstruction_differs_from_retained_inputs")
        return original
    return rows


def retained_pairs(path: Path) -> list[dict[str, Any]]:
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != RETAINED_SHA:
        raise SafeFailure("retained_corpus_digest_changed")
    rows = json.loads(body)
    if not isinstance(rows, list) or len(rows) != 120:
        raise SafeFailure("invalid_retained_count")
    if len({r["id"] for r in rows}) != 120:
        raise SafeFailure("duplicate_retained_pair")
    if sum(r["same_identity"] is True for r in rows) != 45:
        raise SafeFailure("invalid_retained_labels")
    for row in rows:
        request, budget = row["request"], row["budget"]
        if (
            type(row["same_identity"]) is not bool
            or request["questions"] != QUESTION
            or request["model"] != "multilingual"
            or request["max_len"] != 1024
            or budget["after"] > budget["room"]
            or budget["sequence_tokens"] > 1024
            or budget["runtime_state_tokens_dropped"] != 0
            or set(request["state"]) != {"party", "listing", "components"}
        ):
            raise SafeFailure("invalid_retained_input")
        probability(row["response"])
        if not 0 <= number(row["model_probability"]) <= 1:
            raise SafeFailure("invalid_retained_probability")
    return rows


def payload(row: dict[str, Any], provider: str) -> dict[str, Any]:
    # Labels, category, pair IDs, and prior answers stay outside model input.
    request = row["request"]
    shared = {"state": request["state"], "questions": request["questions"]}
    if provider == "jev":
        return {
            **shared,
            "model": MODEL,
            "provider": {
                "only": ["typesafe"],
                "allow_fallbacks": False,
                # The routing schema expresses these caps in USD per million.
                "max_price": {"prompt": 0.042, "completion": 0, "request": 0},
            },
        }
    return {**shared, "model": "multilingual", "max_len": 1024}


def probability(response: dict[str, Any]) -> float:
    try:
        answer = response["answers"]["same_party"]
        result = number(answer["noul"])
        if answer.get("type") != "noul" or not 0 <= result <= 1:
            raise SafeFailure("invalid_probability")
        return result
    except (KeyError, TypeError):
        raise SafeFailure("invalid_probability") from None


def parse_object(body: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SafeFailure("duplicate_response_key")
            result[key] = value
        return result

    try:
        result = json.loads(body, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise SafeFailure("invalid_response_json") from None
    if not isinstance(result, dict):
        raise SafeFailure("invalid_response_json")
    return result


def wire(
    client: httpx.Client,
    url: str,
    *,
    request: dict[str, Any] | None = None,
    key: str | None = None,
) -> tuple[int, dict[str, Any], float]:
    headers = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
    if key:
        headers["Authorization"] = "Bearer " + key
    start = time.perf_counter()
    try:
        with client.stream(
            "POST" if request is not None else "GET",
            url,
            headers=headers,
            content=encoded(request) if request is not None else None,
        ) as response:
            content_encoding = response.headers.get("content-encoding", "identity").strip().lower()
            if content_encoding not in {"", "identity"}:
                raise SafeFailure("unsupported_content_encoding")
            data = bytearray()
            if response.is_stream_consumed:
                # HTTPX MockTransport commonly returns an already-read identity body.
                # Check its size before copying; never use this path for compressed data.
                if len(response.content) > MAX_BODY:
                    raise SafeFailure("response_too_large")
                data.extend(response.content)
            else:
                for chunk in response.iter_raw():
                    if len(data) + len(chunk) > MAX_BODY:
                        raise SafeFailure("response_too_large")
                    data.extend(chunk)
            # Do not persist an upstream reflection of the authorization value.
            if key and key.encode() in data:
                raise SafeFailure("credential_reflection_blocked")
            elapsed = time.perf_counter() - start
            # Error bodies are deliberately neither parsed nor retained.
            if response.status_code != 200:
                return response.status_code, {}, elapsed
            return response.status_code, parse_object(bytes(data)), elapsed
    except httpx.TimeoutException:
        raise SafeFailure("timeout_charge_unknown") from None
    except httpx.HTTPError:
        raise SafeFailure("network_charge_unknown") from None


def verify_pricing(client: httpx.Client) -> dict[str, Any]:
    status, response, _ = wire(client, METADATA)
    if status != 200:
        raise SafeFailure("pricing_unavailable")
    try:
        data = response["data"]
        entries = data["endpoints"]
        if data["id"] != MODEL or not entries:
            raise SafeFailure("pricing_changed")
        for endpoint in entries:
            price = endpoint["pricing"]
            if (
                endpoint["tag"] != "typesafe"
                or endpoint["provider_name"] != "TypeSafe"
                or RETURNED_MODEL not in endpoint["name"]
                or integer(endpoint["max_prompt_tokens"]) > MAX_PROMPT
                or integer(endpoint["max_prompt_tokens"]) == 0
                or amount(price["prompt"]) > PROMPT_PRICE
                or amount(price["completion"]) != 0
                or amount(price.get("request", 0)) != 0
            ):
                raise SafeFailure("pricing_changed")
        return response
    except (KeyError, TypeError):
        raise SafeFailure("pricing_unavailable") from None


def usage(response: dict[str, Any]) -> tuple[Decimal, int, int]:
    try:
        fields = response["usage"]
        cost = amount(fields["cost"])
        input_tokens, output_tokens = (
            integer(fields["input_tokens"]),
            integer(fields["output_tokens"]),
        )
        return cost, input_tokens, output_tokens
    except (KeyError, TypeError):
        raise SafeFailure("usage_cost_missing") from None


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    offset = (len(ordered) - 1) * fraction
    lower, upper = math.floor(offset), math.ceil(offset)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (offset - lower)


def quality(rows: list[dict[str, Any]], probabilities: list[float]) -> dict[str, Any]:
    positives = [p for row, p in zip(rows, probabilities, strict=True) if row["same_identity"]]
    negatives = [p for row, p in zip(rows, probabilities, strict=True) if not row["same_identity"]]
    auc = (
        sum((p > n) + 0.5 * (p == n) for p in positives for n in negatives)
        / (len(positives) * len(negatives))
        if positives and negatives
        else None
    )
    tp = sum(row["same_identity"] and p >= 0.5 for row, p in zip(rows, probabilities, strict=True))
    fp = sum(
        not row["same_identity"] and p >= 0.5 for row, p in zip(rows, probabilities, strict=True)
    )
    return {
        "count": len(rows),
        "positive": len(positives),
        "negative": len(negatives),
        "auroc": auc,
        "brier": statistics.mean(
            (p - row["same_identity"]) ** 2 for row, p in zip(rows, probabilities, strict=True)
        ),
        "accuracy_at_0_5": (tp + len(negatives) - fp) / len(rows),
        "tp": tp,
        "fp": fp,
        "tn": len(negatives) - fp,
        "fn": len(positives) - tp,
        "recall_at_0_5": tp / len(positives) if positives else None,
        "specificity_at_0_5": (len(negatives) - fp) / len(negatives) if negatives else None,
    }


def metrics(
    rows: list[dict[str, Any]], observations: list[dict[str, Any]], timing: str
) -> dict[str, Any]:
    selected = [
        (row, observation)
        for row, observation in zip(rows, observations, strict=False)
        if observation.get("error_code") is None
    ]
    if not selected:
        return {"successful": 0, "timing": timing}
    successful_rows = [row for row, _ in selected]
    probabilities = [observation["probability"] for _, observation in selected]
    durations = [observation["seconds"] for _, observation in selected]
    return {
        "successful": len(selected),
        "timing": timing,
        "quality": quality(successful_rows, probabilities),
        "planted": quality(
            [r for r, _ in selected if r["category"] != "unrelated"],
            [o["probability"] for r, o in selected if r["category"] != "unrelated"],
        ),
        "categories": {
            category: quality(
                [r for r, _ in selected if r["category"] == category],
                [o["probability"] for r, o in selected if r["category"] == category],
            )
            for category in sorted({r["category"] for r, _ in selected})
        },
        "latency_seconds": {
            "total": sum(durations),
            "mean": statistics.mean(durations),
            "p50": percentile(durations, 0.5),
            "p95": percentile(durations, 0.95),
            "p99": percentile(durations, 0.99),
            "max": max(durations),
        },
        "returned_models": dict(Counter(o["returned_model"] for _, o in selected)),
    }


def create_private_directory(path: Path) -> Path:
    workspace = (ROOT / ".workspace").resolve()
    # Existing ignored parent only; never Desktop/iCloud or a public report folder.
    if path.parent.resolve() != workspace or path.exists() or path.is_symlink():
        raise SafeFailure("output_must_be_fresh_workspace_directory")
    path.mkdir(mode=0o700)
    return path


def write_private(path: Path, value: Any) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(encoded(value))
        target.write(b"\n")
        target.flush()
        os.fsync(target.fileno())


def run_provider(
    client: httpx.Client,
    rows: list[dict[str, Any]],
    provider: str,
    output: Path,
    *,
    key: str | None = None,
) -> tuple[list[dict[str, Any]], Decimal, str | None]:
    observations: list[dict[str, Any]] = []
    spent = Decimal(0)
    for index, row in enumerate(rows):
        if provider == "jev" and spent + RESERVATION > CEILING:
            return observations, spent, "spend_ceiling_reserved"
        request = payload(row, provider)
        observation: dict[str, Any] = {
            "pair_index": index,
            "input_sha256": digest({"state": request["state"], "questions": request["questions"]}),
            "error_code": None,
        }
        try:
            status, response, elapsed = wire(
                client,
                DECISIONS if provider == "jev" else "http://127.0.0.1:8009/v1/systemone",
                request=request,
                key=key,
            )
            observation.update(http_status=status, seconds=elapsed)
            if status != 200:
                raise SafeFailure(f"http_{status}")
            observation.update(
                returned_model=response.get("model"),
                provider=response.get("provider"),
                response=response,
            )
            if provider == "jev":
                cost, input_tokens, output_tokens = usage(response)
                spent += cost
                observation.update(
                    cost_usd=str(cost), input_tokens=input_tokens, output_tokens=output_tokens
                )
                if input_tokens > MAX_PROMPT or cost > RESERVATION:
                    raise SafeFailure("published_charge_bound_exceeded")
                if (
                    response.get("model") != RETURNED_MODEL
                    or response.get("provider") != "TypeSafe"
                ):
                    raise SafeFailure("unexpected_model_or_provider")
            observation.update(
                probability=probability(response),
                returned_model=response.get("model"),
                provider=response.get("provider"),
                response=response,
            )
        except SafeFailure as error:
            observation["error_code"] = str(error)
            # An ambiguous/failed paid request consumes its full reservation and stops.
            if provider == "jev" and "cost_usd" not in observation:
                observation["cost_reserved_unknown_usd"] = str(RESERVATION)
                spent += RESERVATION
            observations.append(observation)
            write_private(
                output / f"{provider}-{index:03d}.json", {"request": request, **observation}
            )
            return observations, spent, str(error)
        observations.append(observation)
        write_private(output / f"{provider}-{index:03d}.json", {"request": request, **observation})
        print(
            json.dumps({"provider": provider, "completed": index + 1, "accounted_usd": str(spent)}),
            flush=True,
        )
    return observations, spent, None


def retained_observations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "probability": row["model_probability"],
            "seconds": row["seconds"],
            "returned_model": row["response"]["model"],
            "error_code": None,
        }
        for row in rows
    ]


def build_summary(
    rows: list[dict[str, Any]],
    jev: list[dict[str, Any]],
    laya: list[dict[str, Any]],
    *,
    spent: Decimal,
    stop: str | None,
    fresh_laya: bool,
    watts: float | None,
    tariff: float | None,
    eligible: int | None,
) -> dict[str, Any]:
    paid_known = sum((amount(o["cost_usd"]) for o in jev if "cost_usd" in o), Decimal(0))
    paired = [
        (r, j, local)
        for r, j, local in zip(rows, jev, laya, strict=False)
        if j.get("error_code") is None and local.get("error_code") is None
    ]
    n_paid = sum("cost_usd" in o for o in jev)
    per_comparison = paid_known / n_paid if n_paid else None
    projections = []
    for count in [1000, 10000, 100000, 1000000] + ([eligible] if eligible is not None else []):
        projections.append(
            {
                "eligible_comparisons": count,
                "jev_serial_seconds": (
                    statistics.mean(o["seconds"] for o in jev if o.get("error_code") is None)
                    * count
                    if any(o.get("error_code") is None for o in jev)
                    else None
                ),
                "laya_serial_seconds": (
                    statistics.mean(o["seconds"] for o in laya if o.get("error_code") is None)
                    * count
                    if any(o.get("error_code") is None for o in laya)
                    else None
                ),
                "jev_api_usd": str(per_comparison * count) if per_comparison is not None else None,
                "assumption": (
                    "same average input size and current pricing; "
                    "serial requests; candidate pairs, not parties"
                ),
            }
        )
    result: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "research_only": True,
        "complete_paired_120": len(paired) == 120,
        "stop_code": stop,
        "retained_results_sha256": RETAINED_SHA if rows and "response" in rows[0] else None,
        "input_set_sha256": digest(
            [{"state": r["request"]["state"], "questions": r["request"]["questions"]} for r in rows]
        ),
        "corpus": {
            "pairs": 120,
            "positive": 45,
            "negative": 75,
            "planted": 60,
            "unrelated_controls": 60,
            "ground_truth_sent": False,
        },
        "deterministic": quality(rows, [r["det_score"] / 100 for r in rows]),
        "deterministic_brier_note": (
            "Score/100 is a diagnostic score, not a calibrated probability."
        ),
        "laya": metrics(
            rows,
            laya,
            "fresh paired run" if fresh_laya else "retained earlier run; not simultaneous",
        ),
        "jev": metrics(rows, jev, "new OpenRouter run")
        if jev
        else {"successful": 0, "status": "pending"},
        "paired_count": len(paired),
        "error_counts": dict(Counter(o["error_code"] for o in jev + laya if o.get("error_code"))),
        "fees": {
            "ceiling_usd": str(CEILING),
            "known_usage_cost_usd": str(paid_known),
            "accounted_including_uncertain_reservations_usd": str(spent),
            "maximum_per_request_reservation_usd": str(RESERVATION),
            "priced_requests": n_paid,
            "per_priced_comparison_usd": str(per_comparison)
            if per_comparison is not None
            else None,
            "laya_api_usd": "0",
            "hardware_cost": "unmeasured",
            "account_credit_purchase_fees_and_taxes": "not measured by inference usage.cost",
        },
        "full_book_projections": projections,
        "limits": [
            "Synthetic fixed corpus; not representative production calibration.",
            "No hidden candidate generation or Cartesian party/list expansion.",
            "No product weights/defaults changed.",
            "Pricing guard relies on provider publishing and enforcing its token/routing limits.",
        ],
    }
    if paired:
        same_rows = [r for r, _, _ in paired]
        result["paired_quality"] = {
            "jev": quality(same_rows, [j["probability"] for _, j, _ in paired]),
            "laya": quality(same_rows, [local["probability"] for _, _, local in paired]),
        }
    if watts is not None and tariff is not None:
        durations = [o["seconds"] for o in laya if o.get("error_code") is None]
        assumed_kwh = watts * sum(durations) / 3_600_000
        result["assumed_laya_energy"] = {
            "power_watts": watts,
            "tariff_usd_per_kwh": tariff,
            "kwh": assumed_kwh,
            "usd": assumed_kwh * tariff,
            "note": (
                "Assumed constant incremental wall-time power; "
                "not measured electricity or total server/hardware cost."
            ),
        }
    return result


def self_test() -> None:
    # No network, credentials, corpus writes, or paid requests.
    assert RESERVATION == Decimal("0.001344")
    assert Decimal(".099") + RESERVATION > CEILING
    for value in [True, float("nan"), float("inf"), -1, 1.1]:
        try:
            probability({"answers": {"same_party": {"type": "noul", "noul": value}}})
        except SafeFailure:
            pass
        else:
            raise AssertionError("invalid_probability_accepted")
    for cost_value in [None, True, "NaN", "Infinity", -1]:
        try:
            amount(cost_value)
        except SafeFailure:
            pass
        else:
            raise AssertionError("invalid_cost_accepted")
    try:
        parse_object(b'{"usage":1,"usage":2}')
    except SafeFailure:
        pass
    else:
        raise AssertionError("duplicate_keys_accepted")
    row = {
        "request": {"state": {"party": {"name": "Synthetic"}}, "questions": QUESTION},
        "same_identity": True,
        "category": "exact",
        "id": "not_sent",
    }
    jev, laya = payload(row, "jev"), payload(row, "laya")
    assert jev["state"] == laya["state"] and jev["questions"] == laya["questions"]
    assert not {"id", "same_identity", "category"}.intersection(jev)
    for status in [401, 402, 429, 503, 302]:
        with httpx.Client(
            transport=httpx.MockTransport(
                lambda _, code=status: httpx.Response(code, text="private error")
            )
        ) as client:
            observed, body, _ = wire(client, DECISIONS, request=jev, key="synthetic-private-key")
            assert observed == status and body == {}
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="synthetic-private-key"))
    ) as client:
        try:
            wire(client, DECISIONS, request=jev, key="synthetic-private-key")
        except SafeFailure as error:
            assert str(error) == "credential_reflection_blocked"
        else:
            raise AssertionError("credential_reflection_retained")
    rows = load_pairs()
    assert len(rows) == 120
    if RETAINED.exists():
        original = load_pairs(RETAINED)
        assert quality(original, [r["model_probability"] for r in original])["tp"] == 43
    print(json.dumps({"self_test": "passed", "retained_pairs": len(rows), "network_calls": 0}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retained-results",
        type=Path,
        help="Optional original private results; validates exact reconstructed input identity",
    )
    parser.add_argument(
        "--run-openrouter", action="store_true", help="Explicit paid opt-in; inherited key only"
    )
    parser.add_argument(
        "--run-laya", action="store_true", help="Fresh sequential calls to fixed loopback port 8009"
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--power-watts", type=float)
    parser.add_argument("--tariff-usd-kwh", type=float)
    parser.add_argument("--eligible-comparisons", type=int)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_test:
            self_test()
            return 0
        if (args.power_watts is None) != (args.tariff_usd_kwh is None):
            raise SafeFailure("power_and_tariff_must_be_paired")
        for value in [args.power_watts, args.tariff_usd_kwh]:
            if value is not None and (not math.isfinite(value) or value < 0):
                raise SafeFailure("invalid_energy_assumption")
        if args.eligible_comparisons is not None and args.eligible_comparisons < 0:
            raise SafeFailure("invalid_comparison_projection")
        rows = load_pairs(args.retained_results)
        laya = retained_observations(rows) if args.retained_results else []
        jev: list[dict[str, Any]] = []
        spent, stop = Decimal(0), None
        if args.run_openrouter and not (args.run_laya or args.retained_results):
            raise SafeFailure("paid_comparison_requires_laya_baseline")
        if args.run_openrouter or args.run_laya:
            key = os.environ.get("OPENROUTER_API_KEY") if args.run_openrouter else None
            if args.run_openrouter and not key:
                raise SafeFailure("inherited_openrouter_key_missing")
            output = create_private_directory(
                args.output_dir or ROOT / ".workspace" / ("jev-laya-" + uuid.uuid4().hex)
            )
            with httpx.Client(
                timeout=httpx.Timeout(60, connect=10), follow_redirects=False, trust_env=False
            ) as client:
                if args.run_openrouter:
                    pricing = verify_pricing(client)
                    write_private(output / "pricing.json", pricing)
                    jev, spent, stop = run_provider(client, rows, "jev", output, key=key)
                if args.run_laya:
                    laya, _, laya_stop = run_provider(client, rows, "laya", output)
                    stop = stop or laya_stop
            summary = build_summary(
                rows,
                jev,
                laya,
                spent=spent,
                stop=stop,
                fresh_laya=args.run_laya,
                watts=args.power_watts,
                tariff=args.tariff_usd_kwh,
                eligible=args.eligible_comparisons,
            )
            write_private(output / "summary.json", summary)
            print(
                json.dumps(
                    {
                        "summary_path": str(output / "summary.json"),
                        "paired_count": summary["paired_count"],
                        "complete_paired_120": summary["complete_paired_120"],
                        "stop_code": stop,
                        "known_usage_cost_usd": summary["fees"]["known_usage_cost_usd"],
                    }
                )
            )
            return 0 if stop is None else 1
        print(
            json.dumps(
                build_summary(
                    rows,
                    jev,
                    laya,
                    spent=spent,
                    stop=stop,
                    fresh_laya=False,
                    watts=args.power_watts,
                    tariff=args.tariff_usd_kwh,
                    eligible=args.eligible_comparisons,
                ),
                indent=2,
            )
        )
        return 0
    except SafeFailure as error:
        print(json.dumps({"error_code": str(error)}), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"error_code": "invalid_research_input_or_output"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
