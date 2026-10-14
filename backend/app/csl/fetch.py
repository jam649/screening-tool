"""Bounded, TLS-verified calls to the verified ITA endpoints only."""

from typing import Any

import httpx

from app.csl.parser import BULK_URL, MAX_DOWNLOAD_BYTES, SOURCES_URL, CSLValidationError


class CSLDownloadError(RuntimeError):
    pass


def download_bulk(client: httpx.Client | None = None) -> bytes:
    owned = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=False
    )
    try:
        with client.stream("GET", BULK_URL) as response:
            response.raise_for_status()
            chunks = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise CSLDownloadError("CSL download exceeds the size limit")
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError as exc:
        # HTTP exception strings may contain URLs or headers. Never persist them.
        raise CSLDownloadError("Unable to download the official CSL; check network access") from exc
    finally:
        if owned:
            client.close()


def download_stamps(api_key: str, client: httpx.Client | None = None) -> dict[str, Any]:
    owned = client is None
    client = client or httpx.Client(timeout=15.0, follow_redirects=False)
    try:
        response = client.get(SOURCES_URL, headers={"subscription-key": api_key})
        response.raise_for_status()
        payload = response.json()
        # The authenticated wire format awaits manual verification. Accept only
        # a structurally valid source list; otherwise preserve bulk metadata.
        if isinstance(payload, dict):
            payload = payload.get("sources")
        if not isinstance(payload, list) or not payload:
            raise CSLValidationError("Sources API did not return a valid source list")
        result = {}
        for stamp in payload:
            if not isinstance(stamp, dict) or not isinstance(stamp.get("source"), str):
                raise CSLValidationError("Sources API did not return valid source metadata")
            if stamp["source"] in result:
                raise CSLValidationError("Sources API returned duplicate source metadata")
            result[stamp["source"]] = stamp
        return result
    except (httpx.HTTPError, ValueError) as exc:
        raise CSLDownloadError(
            "Sources API unavailable; using metadata from the bulk download"
        ) from exc
    finally:
        if owned:
            client.close()
