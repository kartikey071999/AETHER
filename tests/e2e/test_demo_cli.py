"""`aether demo capability` runs the scripted workflow against the configured database."""

from pathlib import Path

import pytest
from reliability_support import QuietMonitor
from sqlite_schema import copy_migrated

from aether import main as cli

SECRET = "pw-demo-should-never-appear"


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("AETHER_DATABASE_URL", url)
    monkeypatch.setenv("AETHER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AETHER_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AETHER_WORKSPACE_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setenv("AETHER_ARTIFACT_DIR", str(tmp_path / "artifacts"))


@pytest.mark.slow
def test_demo_capability_prints_the_persisted_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(monkeypatch, tmp_path, f"sqlite+aiosqlite:///{(tmp_path / 'demo.db').as_posix()}")
    copy_migrated(tmp_path / "demo.db")
    # The demo reads the real host; pin it so a loaded machine cannot defer the workflow.
    monkeypatch.setattr("aether.runtime.bootstrap.ResourceMonitor", QuietMonitor)

    cli.main(["demo", "capability", "--timeout", "60"])

    out = capsys.readouterr().out
    assert "scripted mock provider" in out
    assert "status=completed" in out
    assert "LEDGER verdicts: verified" in out
    assert "verification=verified" in out
    outbox = [line for line in out.splitlines() if line.startswith("Outbox: ")]
    assert len(outbox) == 1
    assert Path(outbox[0].removeprefix("Outbox: ")).is_file()


@pytest.mark.slow  # connecting to a closed port takes seconds on Windows
def test_demo_capability_failure_never_prints_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(monkeypatch, tmp_path, f"postgresql://aether:{SECRET}@127.0.0.1:1/aether")

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["demo", "capability", "--timeout", "5"])

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "Demo failed:" in captured.err
    assert SECRET not in captured.out + captured.err


def test_demo_requires_a_scenario_name() -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["demo"])
    assert exit_info.value.code == 2
