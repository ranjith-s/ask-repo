"""The `make db` helper: hosted databases are only checked, local ones may be started."""

import pytest

from scripts import ensure_db
from scripts.ensure_db import Action, decide, target


@pytest.mark.parametrize(
    "url,expected",
    [
        ("postgresql://askrepo:askrepo@localhost:5432/askrepo", ("localhost", 5432)),
        (
            "postgresql://u:p@ep-cool-sun-123456.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
            ("ep-cool-sun-123456.eu-central-1.aws.neon.tech", 5432),
        ),
        (
            "postgresql://postgres.abcd:p%40ss@aws-0-ap-south-1.pooler.supabase.com:6543/postgres",
            ("aws-0-ap-south-1.pooler.supabase.com", 6543),
        ),
        ("postgresql:///askrepo", ("localhost", 5432)),
    ],
)
def test_target_reads_host_and_port_without_needing_credentials(url, expected):
    assert target(url) == expected


@pytest.mark.parametrize(
    "host,reachable,docker,expected",
    [
        ("localhost", True, True, Action.READY),
        ("db.abcd.supabase.co", True, False, Action.READY),
        ("localhost", False, True, Action.START_DOCKER),
        ("127.0.0.1", False, False, Action.NO_LOCAL_DB),
        ("ep-x.neon.tech", False, True, Action.REMOTE_UNREACHABLE),  # never start Docker for a hosted URL
    ],
)
def test_decide(host, reachable, docker, expected):
    assert decide(host, reachable, docker) is expected


def test_unreachable_hosted_db_fails_clearly_without_leaking_the_password(monkeypatch, capsys):
    monkeypatch.setattr(ensure_db, "can_connect", lambda host, port: False)
    started = []
    monkeypatch.setattr(ensure_db.subprocess, "run", lambda *a, **k: started.append(a))
    code = ensure_db.main(["--url", "postgresql://user:s3cret@ep-x.neon.tech/neondb"])
    out = capsys.readouterr().out
    assert code == 1 and not started
    assert "ep-x.neon.tech:5432" in out and "s3cret" not in out


def test_missing_local_db_starts_docker_when_available(monkeypatch):
    monkeypatch.setattr(ensure_db, "can_connect", lambda host, port: False)
    monkeypatch.setattr(ensure_db.shutil, "which", lambda name: "/usr/bin/docker")
    calls = []

    class Done:
        returncode = 0

    monkeypatch.setattr(ensure_db.subprocess, "run", lambda cmd, **k: calls.append(cmd) or Done())
    assert ensure_db.main(["--url", "postgresql://a:b@localhost:5432/x"]) == 0
    assert calls == [["docker", "compose", "up", "-d", "--wait", "db"]]
