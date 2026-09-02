from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import nslab.cli as cli
from nslab.completion import hidden_completion_candidates, render_completion_script
from nslab.manifest import load_manifest, normalized_manifest
from nslab.state import StateSnapshot, StateStore


def _write_manifest(path: Path, *, name: str = "bridge-fdb") -> None:
    path.write_text(
        f"""\
version: 1
name: {name}
topology:
  nodes:
    h1:
      kind: linux
    sw1:
      kind: bridge
      bridge:
        name: br0
        stp: false
        vlan_filtering: false
    h2:
      kind: linux
  links: []
""",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("shell", "registration"),
    [
        ("bash", "complete -F _nslab_completion nslab"),
        ("zsh", "compdef _nslab_completion nslab"),
    ],
)
def test_completion_command_outputs_valid_shell_script(
    shell: str,
    registration: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["completion", shell]) == 0
    script = capsys.readouterr().out

    assert registration in script
    assert "deploy" in script
    assert "--node" in script
    executable = shutil.which(shell)
    if executable is not None:
        subprocess.run(
            [executable, "-n"],
            input=script,
            text=True,
            capture_output=True,
            check=True,
        )


def test_public_help_lists_completion_but_not_hidden_query() -> None:
    help_text = cli._build_parser().format_help()

    assert "completion" in help_text
    assert "__complete" not in help_text


@pytest.mark.parametrize(
    ("words", "cursor_index", "expected"),
    [
        (("nslab", "exec", "--node", "h"), 3, ("h1", "h2")),
        (("nslab", "exec", "--node", "s"), 3, ("sw1",)),
        (("nslab", "exec", "--node", "missing"), 3, ()),
    ],
)
def test_node_completion_uses_default_topology_and_prefix(
    tmp_path: Path,
    words: tuple[str, ...],
    cursor_index: int,
    expected: tuple[str, ...],
) -> None:
    _write_manifest(tmp_path / "nslab.yaml")

    assert (
        hidden_completion_candidates(
            ("nodes", str(cursor_index), *words),
            cwd=tmp_path,
            state_root=tmp_path / "state",
        )
        == expected
    )


def test_node_completion_uses_explicit_topology(tmp_path: Path) -> None:
    topology = tmp_path / "topologies" / "lab.yaml"
    topology.parent.mkdir()
    _write_manifest(topology)
    empty_cwd = tmp_path / "cwd"
    empty_cwd.mkdir()
    words = ("nslab", "exec", "--topo", str(topology), "--node", "s")

    assert hidden_completion_candidates(
        ("nodes", "5", *words),
        cwd=empty_cwd,
        state_root=tmp_path / "state",
    ) == ("sw1",)


def test_node_completion_uses_saved_deployment_without_manifest(tmp_path: Path) -> None:
    topology = tmp_path / "source.yaml"
    _write_manifest(topology, name="source")
    manifest = load_manifest(topology)
    state_root = tmp_path / "state"
    StateStore(state_root).save(
        StateSnapshot(
            schema=1,
            name="saved",
            fingerprint="a" * 64,
            manifest=normalized_manifest(manifest),
            namespaces={},
            interfaces={},
            created_at="2026-09-01T12:00:00+00:00",
        )
    )
    words = ("nslab", "exec", "--name", "saved", "--node", "h")

    assert hidden_completion_candidates(
        ("nodes", "5", *words),
        cwd=tmp_path / "missing-cwd",
        state_root=state_root,
    ) == ("h1", "h2")


def test_deployment_completion_filters_valid_state_filenames(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    for name in ("alpha.json", "bridge-fdb.json", "broken name.json", "beta.txt"):
        (state_root / name).write_text("{}", encoding="utf-8")
    (state_root / "bogus.json").mkdir()

    assert hidden_completion_candidates(
        ("names", "b"),
        cwd=tmp_path,
        state_root=state_root,
    ) == ("bridge-fdb",)


@pytest.mark.parametrize("query", [("names", ""), ("nodes", "3", "nslab", "exec")])
def test_dynamic_completion_silently_degrades_for_missing_data(
    tmp_path: Path,
    query: tuple[str, ...],
) -> None:
    assert (
        hidden_completion_candidates(
            query,
            cwd=tmp_path,
            state_root=tmp_path / "missing-state",
        )
        == ()
    )


def test_hidden_completion_cli_is_silent_and_does_not_require_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "bridge-fdb.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "STATE_ROOT", state_root)
    monkeypatch.setattr(
        cli,
        "_make_backend",
        lambda: (_ for _ in ()).throw(AssertionError("completion initialized backend")),
    )

    assert cli.main(["__complete", "names", "bridge"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "bridge-fdb\n"
    assert captured.err == ""


def test_bash_static_command_and_option_completion(tmp_path: Path) -> None:
    script = tmp_path / "nslab-completion.bash"
    script.write_text(render_completion_script("bash"), encoding="utf-8")
    probe = """\
source "$1"
COMP_WORDS=(nslab gr)
COMP_CWORD=1
_nslab_completion
printf 'command:%s\\n' "${COMPREPLY[@]}"
COMP_WORDS=(nslab graph --f)
COMP_CWORD=2
_nslab_completion
printf 'option:%s\\n' "${COMPREPLY[@]}"
"""

    result = subprocess.run(
        ["bash", "-c", probe, "bash", str(script)],
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout == "command:graph\noption:--format\n"
