"""Remove OAuth callback credentials before routing, errors or URL construction."""

from urllib.parse import parse_qsl

from app.connectors.qbo_common import CALLBACK_PATH


class OAuthCallbackPrivacyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != CALLBACK_PATH:
            return await self.app(scope, receive, send)
        raw = scope.get("query_string", b"")
        scope["query_string"] = b""
        params = {}
        try:
            if len(raw) > 16384:
                raise ValueError
            pairs = parse_qsl(
                raw.decode("ascii"),
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=12,
                errors="strict",
            )
            if len({key for key, _ in pairs}) != len(pairs):
                raise ValueError
            params = dict(pairs)
        except (ValueError, UnicodeError):
            pass
        # Private process memory only; never persistent job parameters or audit.
        scope["_qbo_callback_params"] = params

        async def private_send(message):
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in {b"referrer-policy", b"cache-control"}
                ]
                message["headers"] = headers + [
                    (b"referrer-policy", b"no-referrer"),
                    (b"cache-control", b"no-store"),
                ]
            await send(message)

        return await self.app(scope, receive, private_send)
