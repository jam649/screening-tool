"""Wire, deadline, privacy and real-tokenizer invariants of optional adapters."""

import asyncio
import json
import math
import time
from dataclasses import replace

import httpx
import pytest
from test_review_api import review_seed as review_seed

from app.adjudicators.budget import InputBudgetError, local_state_room, prepare_state
from app.adjudicators.context import build_state
from app.adjudicators.contracts import CLAIM, JEV_ENDPOINT, LAYA_TOKENIZER_SHA256, ModelConfig
from app.adjudicators.http import MAX_RESPONSE_BYTES, HTTPAdjudicator
from app.adjudicators.runner import evaluate
from app.audit import redact_credentials
from app.reports.export import _safe


@pytest.fixture
def laya():
    return ModelConfig(provider="laya", laya_api_key="synthetic-local-key")


def response(probability=0.25, *, model="laya-rl-agent", input_tokens=100):
    return {
        "model": model,
        "answers": {"same_party": {"type": "noul", "noul": probability}},
        "usage": {"input_tokens": input_tokens, "output_tokens": 0},
    }


async def no_sleep(delay):
    return None


def test_fixed_wire_key_never_returned_and_compatibility_noul(laya):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response())

    adapter = HTTPAdjudicator(laya, transport=httpx.MockTransport(handler))
    result = adapter.evaluate({"synthetic": "identity"})
    assert result.model_prob == 0.25 and result.attempts == 1
    assert result.returned_model == "laya-rl-agent" and result.input_tokens == 100
    body = json.loads(requests[0].content)
    assert body == {
        "state": {"synthetic": "identity"},
        "model": "multilingual",
        "max_len": 1024,
        "head_max_len": 256,
        "questions": {"same_party": {"type": "noul", "instructions": CLAIM}},
    }
    assert requests[0].headers["Authorization"] == "Bearer synthetic-local-key"
    assert str(requests[0].url) == "http://127.0.0.1:8009/v1/systemone"
    assert "synthetic-local-key" not in json.dumps(result.metadata())
    assert "synthetic-local-key" not in repr(laya)
    assert adapter.noul("Synthetic context", CLAIM) == 0.25


def test_hosted_fixed_endpoint_pin_and_no_local_fields():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response(model="jev-1.13.0"))

    config = ModelConfig(provider="jev", jev_api_key="synthetic-hosted-key")
    result = HTTPAdjudicator(config, transport=httpx.MockTransport(handler)).evaluate("Synthetic")
    assert result.error_code is None and result.model_prob == 0.25
    assert str(requests[0].url) == JEV_ENDPOINT
    assert set(json.loads(requests[0].content)) == {"state", "model", "questions"}


@pytest.mark.parametrize(
    "bad", [False, True, float("nan"), float("inf"), -0.01, 1.01, "0.25", None]
)
def test_invalid_probabilities_fall_back_without_zero_or_retry(laya, bad):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=json.dumps(response(bad)).encode())

    result = HTTPAdjudicator(laya, transport=httpx.MockTransport(handler)).evaluate("Synthetic")
    assert result.model_prob is None and result.error_code == "invalid_response"
    assert result.attempts == len(calls) == 1


@pytest.mark.parametrize(
    "mutation",
    ["missing_usage", "bool_usage", "wrong_type", "model_mismatch", "duplicate", "oversized"],
)
def test_strict_response_and_size_failures(laya, mutation):
    data = response()
    if mutation == "missing_usage":
        data.pop("usage")
    if mutation == "bool_usage":
        data["usage"]["input_tokens"] = True
    if mutation == "wrong_type":
        data["answers"]["same_party"]["type"] = "choice"
    if mutation == "model_mismatch":
        data["model"] = "synthetic-private-key"
    body = json.dumps(data).encode()
    if mutation == "duplicate":
        body = body.replace(b'"noul": 0.25', b'"noul": 0.25, "noul": 0.75')
    if mutation == "oversized":
        body = b" " * (MAX_RESPONSE_BYTES + 1)
    result = HTTPAdjudicator(
        laya, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    ).evaluate("Synthetic")
    assert result.error_code == "invalid_response" and result.model_prob is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422, 302])
def test_terminal_status_and_redirect_never_retry_or_echo(laya, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://external.example.test"},
            text="synthetic-local-key private response",
        )

    result = HTTPAdjudicator(laya, transport=httpx.MockTransport(handler), sleep=no_sleep).evaluate(
        "Synthetic"
    )
    assert result.attempts == len(calls) == 1 and result.model_prob is None
    assert "synthetic-local-key" not in json.dumps(result.metadata())
    if status == 302:
        assert result.error_code == "redirect_rejected"


@pytest.mark.parametrize("status", [408, 429, 500, 503, 529])
def test_transient_retry_owner_has_exact_bound(laya, status):
    calls, delays = [], []

    async def sleep(delay):
        delays.append(delay)

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    result = HTTPAdjudicator(laya, transport=httpx.MockTransport(handler), sleep=sleep).evaluate(
        "Synthetic"
    )
    assert result.model_prob is None and result.attempts == len(calls) == 3
    assert delays == [0.5, 1.0]


def test_retry_after_obeyed_and_large_delay_cannot_exceed_budget(laya):
    calls, delays = [], []

    async def sleep(delay):
        delays.append(delay)

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "2"})
            if len(calls) == 1
            else httpx.Response(200, json=response())
        )

    result = HTTPAdjudicator(laya, transport=httpx.MockTransport(handler), sleep=sleep).evaluate(
        "Synthetic"
    )
    assert result.model_prob == 0.25 and result.attempts == 2 and delays == [2.0]
    result = HTTPAdjudicator(
        laya,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, headers={"Retry-After": "1000"})
        ),
        sleep=sleep,
    ).evaluate("Synthetic")
    assert result.error_code == "retry_budget_exhausted" and result.attempts == 1
    assert delays == [2.0]


def test_outer_deadline_includes_slow_response(laya):
    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=response())

    start = time.monotonic()
    result = HTTPAdjudicator(
        laya, transport=httpx.MockTransport(handler), total_timeout=0.01
    ).evaluate("Synthetic")
    assert result.error_code == "retry_budget_exhausted" and result.model_prob is None
    assert time.monotonic() - start < 0.2


def test_transport_failure_text_is_never_published(laya):
    def handler(request):
        raise httpx.ReadTimeout("synthetic-local-key private details")

    result = HTTPAdjudicator(laya, transport=httpx.MockTransport(handler), sleep=no_sleep).evaluate(
        "Synthetic"
    )
    assert result.error_code == "transport_unavailable" and result.attempts == 3
    assert "synthetic-local-key" not in json.dumps(result.metadata())


def records():
    party = {
        "id": "private-id",
        "name": "Synthetic Company",
        "party_type": "company",
        "country_iso2": "US",
        "address_lines": ["Synthetic street"],
        "alt_names": ["Best party alias", "Other party alias"],
        "raw": {"private": "Never transmit raw"},
        "api_key": "Never transmit key",
    }
    entry = {
        "name": "Synthetic Listing",
        "entity_type": "company",
        "countries": ["US"],
        "addresses": [{"address": "Synthetic listing street", "city": "Synthetic City"}],
        "alt_names": ["Best entry alias", "Other entry alias"],
        "remarks": "Synthetic remarks",
        "raw": {"private": "Never transmit raw"},
    }
    explanation = {
        "normalized": {
            "compared_party": "synthetic company",
            "compared_entry": "synthetic listing",
        },
        "components": {"name": 85},
        "best_aliases": {"party": "Best party alias", "entry": "Best entry alias"},
        "private": "Never transmit details",
    }
    return party, entry, explanation


def test_context_has_original_normalized_components_without_source_or_labels():
    state = build_state(*records())
    text = json.dumps(state)
    assert "Never transmit" not in text and "private-id" not in text
    assert state["party"]["name"] == "Synthetic Company"
    assert state["listing"]["normalized_name"] == "synthetic listing"
    assert state["components"] == {"name": 85}


def test_real_pinned_tokenizer_fixed_order_truncation_preserves_core_identity():
    party, entry, explanation = records()
    party["address_lines"] = ["Synthetic very long street " * 2000]
    entry["remarks"] = "Synthetic many remarks " * 2000
    party["alt_names"] += ["Synthetic party aliases " * 25 for _ in range(300)]
    entry["alt_names"] += ["Synthetic listing aliases " * 25 for _ in range(300)]
    original = build_state(party, entry, explanation)
    state, proof = prepare_state(original, explanation, "laya")
    assert local_state_room() == 989
    assert proof["removed_fields"] == ["remarks", "address_lines", "extra_aliases"]
    assert proof["exact"] is True and proof["sequence_tokens"] <= 1024
    assert proof["tokenizer_sha256"] == LAYA_TOKENIZER_SHA256
    assert state["party"]["name"] == "Synthetic Company"
    assert state["listing"]["name"] == "Synthetic Listing"
    assert state["party"]["alt_names"] == ["Best party alias"]
    assert state["listing"]["alt_names"] == ["Best entry alias"]
    assert state["listing"]["addresses"][0]["city"] == "Synthetic City"
    assert original["listing"]["remarks"] == entry["remarks"]


def test_essential_oversize_rejects_without_silently_slicing_or_http(laya):
    party, entry, explanation = records()
    party["name"] = "Synthetic primary identity " * 1000
    with pytest.raises(InputBudgetError):
        prepare_state(build_state(party, entry, explanation), explanation, "laya")
    calls = []
    result = evaluate(
        laya, party, entry, explanation, transport=httpx.MockTransport(lambda r: calls.append(r))
    )
    assert result.error_code == "input_budget_exceeded" and result.model_prob is None
    assert result.attempts == 0 and calls == []


def test_hosted_estimate_is_honestly_separate_from_actual_usage():
    state, proof = prepare_state(build_state(*records()), records()[2], "jev")
    assert proof["method"] == "utf8_bytes_conservative_estimate" and proof["exact"] is False
    assert proof["sequence_tokens"] is None and proof["tokenizer_sha256"] is None
    assert proof["after"] <= proof["state_budget"] and state["party"]["name"]


def test_local_usage_mismatch_is_fallback_and_each_call_metadata_is_separate(laya):
    inputs = records()
    _, budget = prepare_state(build_state(*inputs), inputs[2], "laya")
    tokens = budget["sequence_tokens"]
    good = evaluate(
        laya,
        *inputs,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=response(input_tokens=tokens))
        ),
    )
    bad = evaluate(
        laya,
        *inputs,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=response(input_tokens=tokens - 1))
        ),
    )
    assert good.model_prob == 0.25 and good.input_tokens == tokens
    assert bad.error_code == "invalid_response" and bad.model_prob is None
    assert good.error_code is None and good.budget is not bad.budget


def test_valid_public_measurements_survive_but_token_values_still_redact():
    public = {
        "input_tokens": 42,
        "output_tokens": 0,
        "sequence_tokens": 77,
        "laya_max_tokens": 1024,
        "tokenizer_sha256": LAYA_TOKENIZER_SHA256,
    }
    for sanitizer in (redact_credentials, _safe):
        assert sanitizer(public) == public
        private = {
            "input_tokens": "synthetic-key",
            "output_tokens": True,
            "sequence_tokens": math.inf,
            "laya_max_tokens": "synthetic-key",
            "tokenizer_sha256": "synthetic-key",
            "access_token": "synthetic-key",
            "client_state": "synthetic-key",
        }
        assert set(sanitizer(private).values()) == {"[redacted]"}


def test_config_public_fingerprint_changes_on_generation_and_never_has_keys(laya):
    assert laya.scoring_snapshot() != replace(laya, generation=1).scoring_snapshot()
    assert "synthetic-local-key" not in json.dumps(laya.scoring_snapshot())


def test_unreported_public_measurements_remain_null_in_run_and_export_views():
    public = {
        key: None
        for key in (
            "input_tokens",
            "output_tokens",
            "sequence_tokens",
            "laya_max_tokens",
            "tokenizer_sha256",
            "laya_tokenizer_sha256",
        )
    }
    assert redact_credentials(public) == _safe(public) == public
    assert redact_credentials({"access_token": None}) == {"access_token": "[redacted]"}


def test_advertised_compression_is_rejected_before_decoding_or_large_allocation(laya):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"invalid-gzip")
        )

    result = HTTPAdjudicator(laya, transport=httpx.MockTransport(handler)).evaluate("Synthetic")
    assert result.error_code == "invalid_response" and result.model_prob is None
    assert result.attempts == 1
    assert requests[0].headers["accept-encoding"] == "identity"


def test_transport_decoding_exception_also_has_terminal_safe_fallback(laya):
    def handler(request):
        raise httpx.DecodingError("synthetic-local-key private decoding details")

    result = HTTPAdjudicator(laya, transport=httpx.MockTransport(handler)).evaluate("Synthetic")
    assert result.error_code == "invalid_response" and result.attempts == 1
    assert result.model_prob is None and "synthetic-local-key" not in json.dumps(result.metadata())


def test_cleanup_deadline_never_blends_a_failed_result(laya):
    class DelayedClose(httpx.MockTransport):
        async def aclose(self):
            await asyncio.sleep(1)

    transport = DelayedClose(lambda _: httpx.Response(200, json=response(0.7)))
    result = HTTPAdjudicator(laya, transport=transport, total_timeout=0.01).evaluate("Synthetic")
    assert result.error_code == "retry_budget_exhausted"
    assert (
        result.model_prob
        is result.returned_model
        is result.input_tokens
        is result.output_tokens
        is None
    )


def test_defensive_optional_boundary_cannot_abort_screening_on_transport_bug(laya):
    def handler(request):
        raise ArithmeticError("synthetic-local-key unexpected details")

    result = evaluate(laya, *records(), transport=httpx.MockTransport(handler))
    assert result.error_code == "adapter_unavailable" and result.model_prob is None
    assert "synthetic-local-key" not in json.dumps(result.metadata())


def test_malformed_encoding_preserves_normal_screen_and_connection_fallback(
    client, admin, review_seed
):
    def handler(request):
        raise httpx.DecodingError("synthetic-local-key private wire error")

    client.app.state.model_transport = httpx.MockTransport(handler)
    changed = client.patch(
        "/api/v1/settings/model",
        headers={"x-csrf-token": admin["csrf_token"]},
        json={"expected_generation": 0, "provider": "laya", "w_det": 0.4, "w_model": 0.6},
    )
    assert changed.status_code == 200
    tested = client.post(
        "/api/v1/settings/model/test",
        headers={"x-csrf-token": admin["csrf_token"]},
        json={"expected_generation": changed.json()["generation"]},
    )
    assert tested.status_code == 200 and tested.json()["status"] == "failed"
    assert tested.json()["model_prob"] is None and tested.json()["error_code"] == "invalid_response"
    screened = client.post(
        "/api/v1/screen",
        headers={"x-csrf-token": admin["csrf_token"]},
        json={"party_id": review_seed["party_id"]},
    )
    assert screened.status_code == 200 and screened.json()["finished_at"]
    assert screened.json()["model_fallbacks"][0]["error_code"] == "invalid_response"
    from sqlalchemy import select

    from app.models import Hit

    with client.app.state.session_factory() as db:
        hit = db.scalar(select(Hit).where(Hit.run_id == screened.json()["id"]))
        assert (
            hit.model_prob is None
            and hit.final_confidence == hit.det_score
            and hit.bucket == "likely"
        )
    assert "synthetic-local-key" not in tested.text + screened.text


def test_pdf_reads_original_frozen_model_policy_after_current_configuration_changes(
    client, review_seed
):
    import io

    from pypdf import PdfReader

    from app.adjudicators.settings import save_model_config
    from app.reports.export import export_run

    with client.app.state.session_factory() as db:
        save_model_config(
            db,
            client.app.state.config,
            ModelConfig(provider="laya", generation=7, w_det=0.25, w_model=0.75),
        )
        db.commit()
        artifact = export_run(db, review_seed["runs"][0], "pdf")
        try:
            text = " ".join(
                " ".join(
                    page.extract_text() for page in PdfReader(io.BytesIO(artifact.body)).pages
                ).split()
            )
            assert "deterministic weight 1.0; model weight 0.0" in text
            assert "Model provider: none" in text and "Configuration generation: 0" in text
            assert (
                "deterministic weight 0.25" not in text
                and "Configuration generation: 7" not in text
            )
        finally:
            artifact.close()
