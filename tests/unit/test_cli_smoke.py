from __future__ import annotations

from nslab import __version__
from nslab.cli import main


def test_package_has_v1_version() -> None:
    assert __version__ == "0.1.0"


def test_help_returns_success(capsys) -> None:
    assert main(["--help"]) == 0
    output = capsys.readouterr().out
    for command in ("deploy", "destroy", "redeploy", "inspect", "exec", "graph"):
        assert command in output


def test_unknown_command_returns_usage_error(capsys) -> None:
    assert main(["unknown"]) == 2
    assert "invalid choice" in capsys.readouterr().err
