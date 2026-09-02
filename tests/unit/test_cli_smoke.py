from __future__ import annotations

from importlib.metadata import version as distribution_version

from nslab import __version__
from nslab.cli import main
from nslab.version import version_text


def test_package_version_comes_from_distribution_metadata() -> None:
    assert __version__ == distribution_version("nslab")


def test_version_returns_success(capsys) -> None:
    assert main(["--version"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"{version_text()}\n"
    assert captured.err == ""


def test_help_returns_success(capsys) -> None:
    assert main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "--version" in output
    for command in ("deploy", "destroy", "redeploy", "inspect", "exec", "graph"):
        assert command in output


def test_unknown_command_returns_usage_error(capsys) -> None:
    assert main(["unknown"]) == 2
    assert "invalid choice" in capsys.readouterr().err
