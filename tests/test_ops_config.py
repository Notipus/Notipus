"""Guard rails for the repository's CI and deployment configuration.

These files are not exercised by the application test suite, so a regression
in them would otherwise only surface in deployment.
"""

import re
from pathlib import Path


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
