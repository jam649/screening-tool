"""Fresh packaged deployment/operator proof; only explicit synthetic TCP probes."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import socket
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.config import Config
from app.database import create_database_engine, session_factory
from app.models import LoginAttempt, Setting, Source, User
from app.security import login_identifier

HOST = "screening.example.test"
EMAIL = "synthetic-deployment-probe@example.test"
TRUSTED_CLIENT = "203.0.113.10"
UNTRUSTED_CLAIM = "203.0.113.11"
CHECKPOINT = Path("/data/milestone9-deployment-gate/checkpoint.json")


class GateFailure(RuntimeError):
    pass


def require(value, code):
    if not value:
        raise GateFailure(code)


def send_probe(base, client_ip):
    require(
        base in {"http://127.0.0.1:8000", "http://screening-tool:8000"},
        "fixture_endpoint_only",
    )
    with httpx.Client(base_url=base, trust_env=False, follow_redirects=False, timeout=10) as http:
        response = http.post(
            "/api/v1/auth/login",
            headers={
                "Host": HOST,
                "X-Forwarded-For": client_ip,
                "X-Forwarded-Proto": "https",
            },
            json={"email": EMAIL, "password": "Synthetic invalid deployment password!"},
        )
        require(response.status_code == 401, "actual_synthetic_failed_login")
    return {"peer_ip": socket.gethostbyname(socket.gethostname()), "status": 401}


def cli(*args, extra_env=None):
    return subprocess.run(
        ["screening-tool", *args],
        env={**os.environ, **(extra_env or {})},
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )


def run_gate(peer_ip):
    import ipaddress

    ipaddress.ip_address(peer_ip)
    require(not CHECKPOINT.exists(), "fresh_completed_checkpoint_absent")
    config = Config()
    require(config.allowed_hosts == [HOST], "actual_public_only_host_configuration")
    require(
        os.environ.get("FORWARDED_ALLOW_IPS") == "127.0.0.1,::1",
        "explicit_local_proxy_trust",
    )
    checks = []
    with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=10) as http:
        require(
            http.get("/api/v1/health", headers={"Host": HOST}).status_code == 200,
            "public_host_health",
        )
        require(
            http.get("/api/v1/health", headers={"Host": "untrusted.example.test"}).status_code
            == 400,
            "unknown_host_denied",
        )
        public_notice = http.get("/third-party-notices.txt", headers={"Host": HOST})
        require(public_notice.status_code == 200, "actual_served_notices")
    probe = subprocess.run(
        [sys.executable, "-m", "app.healthcheck"],
        env={
            **os.environ,
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "",
        },
        capture_output=True,
        check=False,
        timeout=10,
    )
    require(probe.returncode == 0, "loopback_readiness_ignores_environment_proxies")
    checks.append("public_only_hostname_readiness_without_host_bypass_or_proxy")
    disk_notice = Path("/app/frontend/dist/third-party-notices.txt").read_bytes()
    require(
        public_notice.content
        == disk_notice
        == Path("/app/licenses/third-party/frontend-runtime-LICENSES.txt").read_bytes(),
        "actual_notice_image_and_http_byte_identity",
    )
    require(
        disk_notice.count(b"Package: ") == 11 and b"Permission is hereby granted" in disk_notice,
        "original_runtime_notices_retained",
    )
    require(os.getuid() == 10001, "actual_nonroot_runtime")
    import shutil

    require(
        shutil.which("node") is None and shutil.which("npm") is None,
        "frontend_build_tools_absent",
    )
    checks.append("eleven_original_frontend_notices_packaged_served_and_nonroot")
    engine = create_database_engine(config)
    factory = session_factory(engine, config)
    try:
        with factory() as db:
            require(
                not list(db.scalars(select(User))) and not list(db.scalars(select(Source))),
                "fresh_no_users_or_sources",
            )
            marker = login_identifier(EMAIL, "identity", config)
            before = list(
                db.scalars(select(LoginAttempt).where(LoginAttempt.identity_hash == marker))
            )
            require(len(before) == 1, "one_actual_untrusted_tcp_probe")
            require(
                before[0].address_hash == login_identifier(peer_ip, "address", config),
                "untrusted_proxy_claim_ignored",
            )
            require(
                before[0].address_hash != login_identifier(UNTRUSTED_CLAIM, "address", config),
                "spoofed_forwarded_address_not_used",
            )
            canary = db.scalar(
                select(Setting).where(Setting.key == "__encryption_key_canary")
            ).value
        send_probe("http://127.0.0.1:8000", TRUSTED_CLIENT)
        with factory() as db:
            addresses = set(
                db.scalars(
                    select(LoginAttempt.address_hash).where(LoginAttempt.identity_hash == marker)
                )
            )
            require(
                login_identifier(TRUSTED_CLIENT, "address", config) in addresses,
                "trusted_loopback_forwarding_used",
            )
            require(len(addresses) == 2, "separate_actual_client_rate_identifiers")
        checks.append("actual_trusted_and_untrusted_tcp_forwarded_client_boundaries")
        good = cli("init")
        require(
            good.returncode == 0 and "Database ready" in good.stdout,
            "real_init_matching_key",
        )
        wrong_key = Fernet.generate_key().decode()
        wrong = cli("init", extra_env={"SCREENING_TOOL_SECRET_KEY": wrong_key})
        require(
            wrong.returncode != 0 and "Database ready" not in wrong.stdout,
            "real_init_wrong_key_rejected",
        )
        require(wrong_key not in wrong.stdout + wrong.stderr, "wrong_key_never_echoed")
        with factory() as db:
            require(
                db.scalar(select(Setting).where(Setting.key == "__encryption_key_canary")).value
                == canary,
                "original_key_binding_preserved",
            )
        recover = cli("monitor", "recover")
        require(
            recover.returncode == 0
            and json.loads(recover.stdout)["operation"] == "dispatch_pending",
            "actual_recovery_cli",
        )
        require(
            cli("bc", "subscriptions", "--help").returncode == 0,
            "actual_subscription_cli_available",
        )
        checks.append("real_cli_matching_key_wrong_key_and_scheduled_recovery")
        CHECKPOINT.parent.mkdir(mode=0o700)
        private_outputs = []
        for name, args in [
            ("full.zip", ["export", "--all", "--format", "zip"]),
            ("human.csv", ["export", "training-pairs"]),
        ]:
            output = CHECKPOINT.parent / name
            require(not output.exists(), "new_private_destination")
            created = cli(*args, "--output", str(output))
            require(created.returncode == 0, "real_private_export_cli")
            report = json.loads(created.stdout)
            data = output.read_bytes()
            require(
                stat.S_IMODE(output.stat().st_mode) == 0o600,
                "actual_private_output_mode",
            )
            require(
                len(data) == report["size"]
                and hashlib.sha256(data).hexdigest() == report["sha256"],
                "actual_cli_output_checksum",
            )
            denied = cli(*args, "--output", str(output))
            require(
                denied.returncode != 0 and output.read_bytes() == data,
                "existing_export_preserved",
            )
            private_outputs.append(
                {
                    "format": output.suffix,
                    "mode": "0600",
                    "bytes": len(data),
                    "sha256": report["sha256"],
                }
            )
            if output.suffix == ".zip":
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    manifest = json.loads(archive.read("manifest.json"))["files"]
                    require(
                        set(manifest) == set(archive.namelist()) - {"manifest.json"},
                        "complete_zip_members",
                    )
                    for member, metadata in manifest.items():
                        body = archive.read(member)
                        require(
                            len(body) == metadata["bytes"]
                            and hashlib.sha256(body).hexdigest() == metadata["sha256"],
                            "actual_zip_checksum",
                        )
        checks.append("actual_private_cli_zip_training_checksums_and_no_overwrite")
        result = {
            "milestone": 9,
            "passed": True,
            "validation": "actual-fresh-packaged-deployment-operator",
            "checks": checks,
            "frontend_notice_sha256": hashlib.sha256(disk_notice).hexdigest(),
            "private_exports": private_outputs,
        }
        with CHECKPOINT.open("x") as file:
            json.dump(result, file, sort_keys=True)
        CHECKPOINT.chmod(0o600)
        return result
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--send-untrusted-probe", action="store_true")
    mode.add_argument("--run-fixture-gate", action="store_true")
    parser.add_argument("--untrusted-peer-ip")
    args = parser.parse_args()
    try:
        if args.send_untrusted_probe:
            result = send_probe("http://screening-tool:8000", UNTRUSTED_CLAIM)
        else:
            require(args.untrusted_peer_ip is not None, "explicit_actual_peer_required")
            result = run_gate(args.untrusted_peer_ip)
        print(json.dumps(result, sort_keys=True))
    except GateFailure as error:
        print(json.dumps({"passed": False, "failed_check": str(error)}))
        raise SystemExit(1) from None
    except Exception as error:  # noqa: BLE001 -- only fixed exception types are public
        print(json.dumps({"passed": False, "error_code": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
