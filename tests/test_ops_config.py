"""Guard rails for the repository's CI and deployment configuration.

These files are not exercised by the application test suite, so a regression
in them would otherwise only surface in deployment.
"""

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_dockerfile_takes_trusted_proxies_from_the_environment() -> None:
    """uvicorn's trusted proxies are configurable, not hard-coded to "*".

    A literal ``--forwarded-allow-ips`` flag overrides FORWARDED_ALLOW_IPS,
    so a deployment could not narrow the trusted range without rebuilding.
    """
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    cmd = next(line for line in dockerfile.splitlines() if line.startswith("CMD "))
    assert "--forwarded-allow-ips" not in cmd
    assert "--proxy-headers" in cmd
    assert re.search(r"^ENV FORWARDED_ALLOW_IPS=", dockerfile, re.MULTILINE)


def _compose() -> dict[str, Any]:
    """Load docker-compose.yml.

    Returns:
        The parsed compose file.
    """
    return yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())


def test_compose_publishes_datastores_on_loopback_only() -> None:
    """Postgres and Redis are reachable from this machine, not the network."""
    services = _compose()["services"]
    for name in ("db", "redis"):
        for mapping in services[name].get("ports", []):
            assert str(mapping).startswith("127.0.0.1:"), f"{name}: {mapping}"


def test_compose_datastore_passwords_come_from_the_environment() -> None:
    """Both datastores take their password from a variable the app shares."""
    compose = _compose()
    services = compose["services"]
    app_env = compose["x-common-variables"]

    assert services["db"]["environment"]["POSTGRES_PASSWORD"].startswith(
        "${DB_PASSWORD:-"
    )
    assert services["db"]["environment"]["POSTGRES_PASSWORD"] == app_env["DB_PASSWORD"]

    redis_password = "${REDIS_PASSWORD:-dev_redis_password}"
    assert f'--requirepass "{redis_password}"' in services["redis"]["command"]
    assert services["redis"]["environment"]["REDISCLI_AUTH"] == redis_password
    assert app_env["REDIS_URL"] == f"redis://:{redis_password}@redis:6379/0"


def test_nginx_upstream_matches_the_compose_django_port() -> None:
    """nginx proxies to the port the compose django service listens on."""
    command = _compose()["services"]["django"]["command"]
    port = command[command.index("--port") + 1]
    nginx = (REPO_ROOT / "nginx.conf").read_text()
    upstream = re.search(r"server notipus_django:(\d+);", nginx)

    assert upstream, "nginx.conf no longer names the notipus_django upstream"
    assert upstream.group(1) == port
