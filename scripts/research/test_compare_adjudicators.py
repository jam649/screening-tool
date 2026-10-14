"""Network-free regressions for the paid research harness, not product adapters."""

from decimal import Decimal

import compare_adjudicators as comparison
import httpx
import pytest


def response(*, cost="0.000019992", probability=0.5):
    return {
        "answers": {"same_party": {"type": "noul", "noul": probability}},
        "model": comparison.RETURNED_MODEL,
        "provider": "TypeSafe",
        "usage": {"cost": cost, "input_tokens": 476, "output_tokens": 70},
    }


def pricing():
    return {
        "data": {
            "id": comparison.MODEL,
            "endpoints": [
                {
                    "tag": "typesafe",
                    "name": "TypeSafe | " + comparison.RETURNED_MODEL,
                    "provider_name": "TypeSafe",
                    "max_prompt_tokens": 32000,
                    "pricing": {"prompt": "0.000000042", "completion": "0"},
                }
            ],
        }
    }


@pytest.fixture
def pairs():
    return comparison.load_pairs()


def test_public_reconstruction_has_exact_pinned_inputs_without_private_results(pairs):
    assert len(pairs) == 120
    assert sum(r["same_identity"] for r in pairs) == 45
    assert (
        comparison.digest(
            [
                {"state": r["request"]["state"], "questions": r["request"]["questions"]}
                for r in pairs
            ]
        )
        == comparison.INPUT_SHA
    )
    assert max(r["budget"]["sequence_tokens"] for r in pairs) == 997
    for row in pairs:
        assert row["budget"]["runtime_state_tokens_dropped"] == 0
        hosted, local = comparison.payload(row, "jev"), comparison.payload(row, "laya")
        assert comparison.encoded(hosted["state"]) == comparison.encoded(local["state"])
        assert hosted["questions"] == local["questions"] == comparison.QUESTION
        assert set(hosted) == {"state", "questions", "model", "provider"}
        assert not {"same_identity", "category", "id", "det_score"}.intersection(hosted["state"])


def test_retained_request_identity_when_original_file_available(pairs):
    if not comparison.RETAINED.exists():
        pytest.skip("Earlier private measurements are not part of a fresh clone")
    retained = comparison.load_pairs(comparison.RETAINED)
    assert [r["request"] for r in pairs] == [r["request"] for r in retained]
    metrics = comparison.quality(retained, [r["model_probability"] for r in retained])
    assert metrics["auroc"] == pytest.approx(0.3191111111111111)
    assert metrics["tp"] == 43 and metrics["fp"] == 74


@pytest.mark.parametrize("status", [401, 402, 429, 503, 302])
def test_paid_error_has_one_call_no_retry_no_error_body_saved(status, pairs, tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private upstream description")

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        rows, spent, stop = comparison.run_provider(client, pairs, "jev", tmp_path, key="dummy-key")
    assert len(calls) == 1 and stop == f"http_{status}"
    assert spent == comparison.RESERVATION
    assert rows[0]["cost_reserved_unknown_usd"] == str(comparison.RESERVATION)
    assert "private upstream description" not in (tmp_path / "jev-000.json").read_text()
    assert "dummy-key" not in (tmp_path / "jev-000.json").read_text()


def test_unknown_timeout_reserves_then_stops_without_retry(pairs, tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("never expose this private diagnostic")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows, spent, stop = comparison.run_provider(client, pairs, "jev", tmp_path)
    assert len(calls) == 1 and stop == "timeout_charge_unknown"
    assert spent == comparison.RESERVATION
    assert rows[0]["error_code"] == stop
    assert "never expose" not in (tmp_path / "jev-000.json").read_text()


def test_hard_ceiling_reserves_published_maximum_before_next_call(pairs, tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response(cost=str(comparison.RESERVATION)))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows, spent, stop = comparison.run_provider(client, pairs, "jev", tmp_path)
    assert len(calls) == len(rows) == 74
    assert spent == Decimal(".099456") <= Decimal(".10")
    assert spent + comparison.RESERVATION > Decimal(".10")
    assert stop == "spend_ceiling_reserved"
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in tmp_path.iterdir())


def test_actual_cost_releases_conservative_reservation_for_all120(pairs, tmp_path):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response()))
    ) as client:
        rows, spent, stop = comparison.run_provider(client, pairs, "jev", tmp_path)
    assert len(rows) == 120 and stop is None
    assert spent == Decimal(".002399040")
    assert all(r["returned_model"] == comparison.RETURNED_MODEL for r in rows)


@pytest.mark.parametrize("cost", [None, True, "NaN", "-1"])
def test_invalid_or_missing_usage_fails_closed_with_full_reservation(cost, pairs, tmp_path):
    body = response(cost=cost)
    if cost is None:
        del body["usage"]["cost"]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        rows, spent, stop = comparison.run_provider(client, pairs, "jev", tmp_path)
    assert len(rows) == 1 and stop is not None
    assert spent == comparison.RESERVATION


def test_invalid_probability_keeps_actual_charge_and_raw_private_response(pairs, tmp_path):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=response(probability=True))
        )
    ) as client:
        rows, spent, stop = comparison.run_provider(client, pairs, "jev", tmp_path)
    assert len(rows) == 1 and stop == "invalid_number"
    assert spent == Decimal(".000019992")
    assert rows[0]["response"]["answers"]["same_party"]["noul"] is True


@pytest.mark.parametrize(
    "field,value", [("prompt", ".000000043"), ("completion", ".000000001"), ("request", ".01")]
)
def test_published_price_increase_fails_before_paid_request(field, value):
    body = pricing()
    body["data"]["endpoints"][0]["pricing"][field] = value
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(comparison.SafeFailure, match="pricing_changed"):
            comparison.verify_pricing(client)
    assert len(calls) == 1 and calls[0].method == "GET"


def test_private_directory_refuses_existing_or_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(comparison, "ROOT", tmp_path)
    workspace = tmp_path / ".workspace"
    workspace.mkdir()
    path = comparison.create_private_directory(workspace / "fresh")
    assert path.stat().st_mode & 0o777 == 0o700
    with pytest.raises(comparison.SafeFailure):
        comparison.create_private_directory(path)
    with pytest.raises(comparison.SafeFailure):
        comparison.create_private_directory(tmp_path / "public")
    comparison.write_private(path / "report.json", {"safe": True})
    assert (path / "report.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        comparison.write_private(path / "report.json", {"overwrite": True})


def test_summary_distinguishes_old_latency_assumed_energy_and_candidate_pairs(pairs):
    original = [
        {"probability": 0.5, "seconds": 0.1, "returned_model": "laya-rl-agent", "error_code": None}
        for _ in pairs
    ]
    hosted = [
        {
            "probability": 0.5,
            "seconds": 0.2,
            "returned_model": comparison.RETURNED_MODEL,
            "cost_usd": ".00001",
            "error_code": None,
        }
        for _ in pairs
    ]
    summary = comparison.build_summary(
        pairs,
        hosted,
        original,
        spent=Decimal(".0012"),
        stop=None,
        fresh_laya=False,
        watts=100,
        tariff=0.20,
        eligible=12000,
    )
    assert summary["complete_paired_120"]
    assert summary["laya"]["timing"] == "retained earlier run; not simultaneous"
    assert summary["assumed_laya_energy"]["usd"] == pytest.approx(0.00006666666667)
    assert summary["full_book_projections"][-1]["jev_api_usd"] == "0.12000"
    assert summary["fees"]["hardware_cost"] == "unmeasured"
    assert "party" not in summary and "request" not in summary and "response" not in summary


def test_actual_unexpected_charge_is_reported_truthfully_and_stops(pairs, tmp_path):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response(cost=".1")))
    ) as client:
        rows, spent, stop = comparison.run_provider(client, pairs, "jev", tmp_path)
    assert len(rows) == 1 and stop == "published_charge_bound_exceeded"
    assert spent == Decimal(".1")
    assert rows[0]["cost_usd"] == "0.1"
    assert "cost_reserved_unknown_usd" not in rows[0]


class ObservedRawStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read_count = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    def close(self):
        self.closed = True


def test_identity_header_and_raw_identity_stream(monkeypatch):
    body = comparison.encoded(response())
    stream = ObservedRawStream([body[:10], body[10:]])
    seen = []

    def handler(request):
        seen.append(request.headers["accept-encoding"])
        return httpx.Response(200, headers={"content-encoding": "identity"}, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        status, parsed, _ = comparison.wire(client, comparison.DECISIONS, request={"dummy": True})
    assert status == 200 and parsed == response()
    assert seen == ["identity"] and stream.read_count == 2 and stream.closed


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "identity, gzip"])
def test_compressed_responses_rejected_before_read_or_decode(encoding):
    import gzip

    # A small compressed body would expand beyond the limit if a decoder ran.
    stream = ObservedRawStream([gzip.compress(b"x" * (comparison.MAX_BODY + 1))])
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-encoding": encoding}, stream=stream
            )
        )
    ) as client:
        with pytest.raises(comparison.SafeFailure, match="unsupported_content_encoding"):
            comparison.wire(client, comparison.DECISIONS, request={"dummy": True})
    assert stream.read_count == 0 and stream.closed


def test_raw_response_limit_is_cumulative_and_stops_before_next_chunk(monkeypatch):
    monkeypatch.setattr(comparison, "MAX_BODY", 32)
    stream = ObservedRawStream([b"x" * 20, b"x" * 13, b"must not read"])
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as client:
        with pytest.raises(comparison.SafeFailure, match="response_too_large"):
            comparison.wire(client, comparison.DECISIONS, request={"dummy": True})
    assert stream.read_count == 2 and stream.closed


def test_raw_identity_response_exactly_at_limit_is_accepted(monkeypatch):
    body = b'{"safe":"synthetic"}'
    monkeypatch.setattr(comparison, "MAX_BODY", len(body))
    stream = ObservedRawStream([body[:5], body[5:]])
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as client:
        status, parsed, _ = comparison.wire(client, comparison.DECISIONS, request={"dummy": True})
    assert status == 200 and parsed == {"safe": "synthetic"}


@pytest.mark.parametrize("oversized", [False, True])
def test_already_consumed_identity_mock_body_obeys_same_bound(monkeypatch, oversized):
    body = comparison.encoded(response())
    monkeypatch.setattr(comparison, "MAX_BODY", len(body) - int(oversized))
    captured = []

    def handler(request):
        prepared = httpx.Response(200, content=body)
        assert prepared.is_stream_consumed
        captured.append(prepared)
        return prepared

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        if oversized:
            with pytest.raises(comparison.SafeFailure, match="response_too_large"):
                comparison.wire(client, comparison.DECISIONS, request={"dummy": True})
        else:
            status, parsed, _ = comparison.wire(
                client, comparison.DECISIONS, request={"dummy": True}
            )
            assert status == 200 and parsed == response()
    assert captured[0].is_closed


def test_paid_encoded_response_reserves_charge_and_never_retries(pairs, tmp_path):
    streams = []

    def handler(request):
        stream = ObservedRawStream([b"compressed private body"])
        streams.append(stream)
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows, spent, stop = comparison.run_provider(client, pairs, "jev", tmp_path)
    assert len(rows) == len(streams) == 1 and spent == comparison.RESERVATION
    assert stop == "unsupported_content_encoding"
    assert streams[0].read_count == 0
    assert "compressed private body" not in (tmp_path / "jev-000.json").read_text()
