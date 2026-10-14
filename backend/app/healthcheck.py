"""Loopback readiness with the deployment's explicitly allowed Host header."""

from urllib.request import ProxyHandler, Request, build_opener

from app.config import Config


def main() -> None:
    try:
        config = Config()
        request = Request(
            "http://127.0.0.1:8000/api/v1/health",
            headers={"Host": config.allowed_hosts[0]},
        )
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            if response.status != 200:
                raise ValueError("Unhealthy response")
    except Exception:
        raise SystemExit("Health check failed") from None


if __name__ == "__main__":
    main()
