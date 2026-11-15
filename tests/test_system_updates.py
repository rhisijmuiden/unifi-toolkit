"""Tests for the Info / update API (app/routers/system.py)."""
import json
import subprocess

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import system

XHR = {"X-Requested-With": "XMLHttpRequest"}


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "HOME": str(cwd), "PATH": "/usr/bin:/bin:/usr/local/bin"})


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """An 'origin' with main and test branches, and a checkout of main."""
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "First")
    _git(src, "checkout", "-q", "-b", "test")
    (src / "pyproject.toml").write_text('[project]\nversion = "1.1.0"\n')
    _git(src, "commit", "-qam", "Test release")
    _git(src, "checkout", "-q", "main")

    app_dir = tmp_path / "app"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(src), str(app_dir)], check=True)
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(system, "APP_DIR", app_dir)
    monkeypatch.setattr(system, "STATUS_FILE", state / "update-status.json")
    monkeypatch.setattr(system, "REQUEST_FILE", state / "update-request")
    monkeypatch.setattr(system, "LOG_FILE", state / "update.log")
    monkeypatch.setattr(system, "UPDATER_UNIT", state / "unit.path")
    system._last_fetch.clear()
    return src, app_dir, state


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        yield c


def test_version_from_pyproject():
    assert system._version_from_pyproject('name = "x"\nversion = "2.3.4"\n') == "2.3.4"
    assert system._version_from_pyproject(None) is None


async def test_info_reports_channel(repos, client):
    r = await client.get("/api/system/info")
    d = r.json()
    assert d["channel"] == "main"
    assert d["channel_label"] == "Production"
    assert d["updater_available"] is False  # no updater unit installed


async def test_up_to_date_on_main(repos, client):
    d = (await client.get("/api/system/updates?channel=main&refresh=1")).json()
    assert d["checked"] is True
    assert d["update_available"] is False
    assert d["commits_behind"] == 0


async def test_new_commit_on_main_is_offered(repos, client):
    src, _, _ = repos
    (src / "README").write_text("x")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "Fix something")
    d = (await client.get("/api/system/updates?channel=main&refresh=1")).json()
    assert d["update_available"] is True
    assert d["commits_behind"] == 1
    assert d["changes"][0]["subject"] == "Fix something"


async def test_switching_to_test_channel(repos, client):
    d = (await client.get("/api/system/updates?channel=test&refresh=1")).json()
    assert d["switching_channel"] is True
    assert d["latest_version"] == "1.1.0"


async def test_unknown_channel_refused(repos, client):
    r = await client.get("/api/system/updates?channel=evil")
    assert r.status_code == 400
    r = await client.post("/api/system/update", json={"channel": "evil"}, headers=XHR)
    assert r.status_code == 400


async def test_update_needs_updater(repos, client):
    r = await client.post("/api/system/update", json={"channel": "main"}, headers=XHR)
    assert r.status_code == 409


async def test_update_writes_request(repos, client):
    _, _, state = repos
    (state / "unit.path").write_text("")
    r = await client.post("/api/system/update", json={"channel": "test"}, headers=XHR)
    assert r.status_code == 200
    assert (state / "update-request").read_text().strip() == "test"
    assert json.loads((state / "update-status.json").read_text())["state"] == "queued"
    # A second request while one is running is refused.
    (state / "update-status.json").write_text(json.dumps({"state": "running"}))
    r = await client.post("/api/system/update", json={"channel": "test"}, headers=XHR)
    assert r.status_code == 409


async def test_update_requires_csrf_header(repos, client):
    r = await client.post("/api/system/update", json={"channel": "main"})
    assert r.status_code == 403
