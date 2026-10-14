"""One bounded, body-private HTTP owner for the verified System One wire."""

import asyncio
import json
import math
import re
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime

import httpx

from app.adjudicators.contracts import CLAIM, JEV_ENDPOINT, Evaluation, ModelConfig

MAX_RESPONSE_BYTES = 1_048_576
MAX_ATTEMPTS = 3
TOTAL_TIMEOUT = 30.0


class AdjudicatorError(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate response field")
        result[key] = value
    return result


def _response_result(raw: bytes, config: ModelConfig) -> tuple[float, str, int, int]:
    data = json.loads(raw, object_pairs_hook=_unique_object)
    answer = data["answers"]["same_party"]
    value, model = answer["noul"], data["model"]
    if (
        answer["type"] != "noul"
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ValueError("Invalid Noul response")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", model):
        raise ValueError("Invalid returned model")
    if config.provider == "jev" and model != config.jev_model:
        raise ValueError("Pinned model mismatch")
    if config.provider == "laya" and model not in {
        "laya-rl-agent",
        config.laya_model,
        "convaiinnovations/laya",
    }:
        raise ValueError("Unsupported local model response")
    usage = data["usage"]
    counts = usage["input_tokens"], usage["output_tokens"]
    if any(
        isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 10_000_000 for v in counts
    ):
        raise ValueError("Invalid usage response")
    return float(value), model, *counts


def _status_code(status: int) -> str:
    if status in {401, 403}:
        return "authentication_rejected"
    if status == 429:
        return "rate_limited"
    if status == 529:
        return "provider_overloaded"
    if status in {400, 413, 422}:
        return "invalid_request"
    if 300 <= status < 400:
        return "redirect_rejected"
    return "provider_unavailable"


def _retry_delay(headers: httpx.Headers, attempt: int) -> float:
    delay = min(5.0, 0.5 * 2 ** (attempt - 1))
    try:
        if "retry-after-ms" in headers:
            delay = float(headers["retry-after-ms"]) / 1000
        elif "retry-after" in headers:
            value = headers["retry-after"]
            try:
                delay = float(value)
            except ValueError:
                delay = parsedate_to_datetime(value).timestamp() - time.time()
        if not math.isfinite(delay):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        return min(5.0, 0.5 * 2 ** (attempt - 1))
    return max(0.0, delay)


class HTTPAdjudicator:
    """No mutable last response; each invocation returns its own immutable result."""

    def __init__(
        self,
        config: ModelConfig,
        *,
        transport=None,
        total_timeout=TOTAL_TIMEOUT,
        sleep: Callable = asyncio.sleep,
    ):
        self.config, self.transport = config, transport
        self.total_timeout, self.sleep = total_timeout, sleep

    def noul(self, context: str, claim: str) -> float:
        if claim != CLAIM:
            raise AdjudicatorError("Use the pinned identity claim")
        from app.adjudicators.budget import local_state_room, local_tokens

        if (self.config.provider == "laya" and local_tokens(context) > local_state_room()) or (
            self.config.provider == "jev" and len(context.encode("utf-8")) > 31_800
        ):
            raise AdjudicatorError("input_budget_exceeded")
        result = self.evaluate(context)
        if result.model_prob is None:
            raise AdjudicatorError(result.error_code or "model_unavailable")
        return result.model_prob

    def evaluate(self, state, *, budget: dict | None = None) -> Evaluation:
        return asyncio.run(self._evaluate(state, budget=budget or {}))

    async def _evaluate(self, state, *, budget: dict) -> Evaluation:
        config = self.config
        started = time.monotonic()
        attempts = 0
        error: str | None = "provider_unavailable"
        model = None
        probability = input_tokens = output_tokens = None
        if config.provider == "jev":
            endpoint, key = JEV_ENDPOINT, config.jev_api_key
        elif config.provider == "laya":
            from app.adjudicators.settings import validate_laya_base_url

            endpoint = validate_laya_base_url(config.laya_base_url).rstrip("/") + "/v1/systemone"
            key = config.laya_api_key
        else:
            return Evaluation(provider="none", requested_model=None, error_code="model_disabled")
        payload = {
            "state": state,
            "model": config.requested_model,
            "questions": {"same_party": {"type": "noul", "instructions": CLAIM}},
        }
        if config.provider == "laya":
            payload.update(max_len=1024, head_max_len=256)
        headers = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
        if key:
            headers["Authorization"] = "Bearer " + key
        if config.provider == "jev" and not key:
            return Evaluation(
                provider="jev",
                requested_model=config.requested_model,
                error_code="key_not_configured",
                budget=budget,
            )
        try:
            async with asyncio.timeout(self.total_timeout):
                async with httpx.AsyncClient(
                    transport=self.transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(10.0, connect=3.0),
                ) as client:
                    for attempts in range(1, MAX_ATTEMPTS + 1):
                        retry = False
                        delay = min(5.0, 0.5 * 2 ** (attempts - 1))
                        try:
                            async with client.stream(
                                "POST", endpoint, json=payload, headers=headers
                            ) as response:
                                if response.status_code != 200:
                                    error = _status_code(response.status_code)
                                    retry = (
                                        response.status_code in {408, 429}
                                        or 500 <= response.status_code <= 599
                                    )
                                    delay = _retry_delay(response.headers, attempts)
                                else:
                                    # Bound raw bytes before any decompressor can allocate.
                                    # The request explicitly asks for identity encoding.
                                    if response.headers.get(
                                        "content-encoding", "identity"
                                    ).lower().strip() not in {"", "identity"}:
                                        raise ValueError("Unsupported response encoding")
                                    content = bytearray()
                                    if response.is_stream_consumed:
                                        if len(response.content) > MAX_RESPONSE_BYTES:
                                            raise ValueError("Oversized response")
                                        content.extend(response.content)
                                    else:
                                        async for chunk in response.aiter_raw():
                                            if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                                                raise ValueError("Oversized response")
                                            content.extend(chunk)
                                    probability, model, input_tokens, output_tokens = (
                                        _response_result(bytes(content), config)
                                    )
                                    if config.provider == "laya" and (
                                        input_tokens > 1024
                                        or (
                                            budget.get("sequence_tokens") is not None
                                            and input_tokens != budget["sequence_tokens"]
                                        )
                                    ):
                                        probability = model = input_tokens = output_tokens = None
                                        raise ValueError("Local tokenizer/sequence usage mismatch")
                                    error = None
                                    break
                        except (httpx.TimeoutException, httpx.TransportError):
                            error, retry = "transport_unavailable", True
                        except (
                            httpx.HTTPError,
                            ValueError,
                            TypeError,
                            KeyError,
                            RecursionError,
                            OverflowError,
                        ):
                            error, retry = "invalid_response", False
                        if not retry or attempts == MAX_ATTEMPTS:
                            break
                        if delay >= self.total_timeout - (time.monotonic() - started):
                            error = "retry_budget_exhausted"
                            break
                        await self.sleep(delay)
        except TimeoutError:
            error = "retry_budget_exhausted"
        except httpx.HTTPError:
            error = "transport_unavailable"
        if error is not None:
            probability = model = input_tokens = output_tokens = None
        return Evaluation(
            provider=config.provider,
            requested_model=config.requested_model,
            model_prob=probability,
            returned_model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            attempts=attempts,
            elapsed_ms=round((time.monotonic() - started) * 1000),
            error_code=error,
            budget=budget,
        )
