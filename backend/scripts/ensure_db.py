"""Make sure the database is reachable before `make dev`, `make test` or `make eval`.

- Hosted database (Neon, Supabase, RDS…): only check it is reachable. Never start anything.
- Local database already running (native install or Docker): nothing to do.
- Local database not running: start the Docker one if Docker is installed, else explain.

Usage (from backend/):  uv run python -m scripts.ensure_db [--url URL]
Without --url it uses DATABASE_URL as the app sees it (environment, then .env files).
"""

import argparse
import shutil
import socket
import subprocess
import sys
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
REPO_ROOT = Path(__file__).resolve().parents[2]


class Action(Enum):
    READY = "ready"
    START_DOCKER = "start_docker"
    NO_LOCAL_DB = "no_local_db"
    REMOTE_UNREACHABLE = "remote_unreachable"


def target(url: str) -> tuple[str, int]:
    """Host and port from a postgres:// URL (credentials are never printed)."""
    parts = urlsplit(url)
    return parts.hostname or "localhost", parts.port or 5432


def is_local(host: str) -> bool:
    return host in LOCAL_HOSTS


def decide(host: str, reachable: bool, docker_available: bool) -> Action:
    if reachable:
        return Action.READY
    if not is_local(host):
        return Action.REMOTE_UNREACHABLE
    return Action.START_DOCKER if docker_available else Action.NO_LOCAL_DB


def can_connect(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Postgres URL to check (default: the app's DATABASE_URL)")
    args = parser.parse_args(argv)
    if args.url:
        url = args.url
    else:
        from app.config import Settings

        url = Settings().database_url

    host, port = target(url)
    where = f"{host}:{port}"
    action = decide(host, can_connect(host, port), shutil.which("docker") is not None)

    if action is Action.READY:
        kind = "local" if is_local(host) else "hosted"
        print(f"Using the {kind} Postgres at {where}.")
        return 0
    if action is Action.START_DOCKER:
        print(f"No Postgres on {where}; starting the Docker one.")
        return subprocess.run(["docker", "compose", "up", "-d", "--wait", "db"], cwd=REPO_ROOT).returncode
    if action is Action.NO_LOCAL_DB:
        print(f"No Postgres on {where} and Docker is not installed.")
        print("Install Postgres 16 + pgvector (README: Running without Docker),")
        print("or set DATABASE_URL to a hosted database (README: Using a hosted Postgres).")
        return 1
    print(f"Cannot reach the Postgres at {where} from DATABASE_URL.")
    print("Check the host and port, your network, and that the database is not paused.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
