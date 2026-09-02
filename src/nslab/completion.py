from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from nslab.manifest import NAME_PATTERN, Manifest, load_manifest
from nslab.state import StateStore

LIFECYCLE_COMMANDS = ("deploy", "destroy", "redeploy")
PUBLIC_COMMANDS = (*LIFECYCLE_COMMANDS, "inspect", "exec", "graph", "completion")
COMPLETION_SHELLS = ("bash", "zsh")
INSPECT_FORMATS = ("table", "json")
GRAPH_FORMATS = ("tree", "box", "mermaid", "dot", "json")

_SELECTION_OPTIONS = ("-h", "--help", "-t", "--topo", "-n", "--name", "--debug")
_COMMAND_OPTIONS: Mapping[str, tuple[str, ...]] = {
    **{command: _SELECTION_OPTIONS for command in LIFECYCLE_COMMANDS},
    "inspect": (*_SELECTION_OPTIONS, "--format"),
    "exec": (*_SELECTION_OPTIONS, "--node", "--"),
    "graph": (*_SELECTION_OPTIONS, "--format", "--detail"),
    "completion": ("-h", "--help"),
}

_BASH_TEMPLATE = r"""# bash completion for nslab
_nslab_complete_words()
{
    local values=$1 current=$2
    mapfile -t COMPREPLY < <(compgen -W "$values" -- "$current")
}

_nslab_completion()
{
    local current previous command word
    local -i index separator=-1
    local -a candidates

    current=${COMP_WORDS[COMP_CWORD]}
    previous=
    if (( COMP_CWORD > 0 )); then
        previous=${COMP_WORDS[COMP_CWORD-1]}
    fi

    command=
    for ((index = 1; index < COMP_CWORD; index++)); do
        word=${COMP_WORDS[index]}
        case "$word" in
            @COMMAND_PATTERN@)
                command=$word
                break
                ;;
        esac
    done

    if [[ -z $command ]]; then
        _nslab_complete_words "@ROOT_CANDIDATES@" "$current"
        return
    fi

    if [[ $command == exec ]]; then
        for ((index = 1; index < COMP_CWORD; index++)); do
            if [[ ${COMP_WORDS[index]} == -- ]]; then
                separator=$index
                break
            fi
        done
        if (( separator >= 0 )); then
            if declare -F _command_offset >/dev/null; then
                _command_offset "$((separator + 1))"
            elif (( COMP_CWORD == separator + 1 )); then
                mapfile -t COMPREPLY < <(compgen -c -- "$current")
            else
                COMPREPLY=()
                compopt -o default -o bashdefault
            fi
            return
        fi
    fi

    if [[ $command == completion ]]; then
        _nslab_complete_words "@SHELL_CANDIDATES@" "$current"
        return
    fi

    case "$previous" in
        -t|--topo)
            mapfile -t COMPREPLY < <(compgen -f -- "$current")
            compopt -o filenames
            return
            ;;
        -n|--name)
            mapfile -t COMPREPLY < <(
                command "${COMP_WORDS[0]}" __complete names "$current" 2>/dev/null
            )
            return
            ;;
        --node)
            if [[ $command == exec ]]; then
                mapfile -t COMPREPLY < <(
                    command "${COMP_WORDS[0]}" __complete nodes \
                        "$COMP_CWORD" "${COMP_WORDS[@]}" 2>/dev/null
                )
            fi
            return
            ;;
        --format)
            case "$command" in
                inspect) _nslab_complete_words "@INSPECT_FORMATS@" "$current" ;;
                graph) _nslab_complete_words "@GRAPH_FORMATS@" "$current" ;;
            esac
            return
            ;;
    esac

    case "$command" in
@BASH_OPTION_CASES@
    esac
    _nslab_complete_words "${candidates[*]}" "$current"
}

complete -F _nslab_completion nslab
"""

_ZSH_TEMPLATE = r"""#compdef nslab
# zsh completion for nslab
_nslab_completion()
{
    local current previous command word
    local -i index separator=0
    local -a candidates

    current=${words[CURRENT]}
    previous=
    if (( CURRENT > 1 )); then
        previous=${words[CURRENT-1]}
    fi

    command=
    for ((index = 2; index < CURRENT; index++)); do
        word=${words[index]}
        case "$word" in
            @COMMAND_PATTERN@)
                command=$word
                break
                ;;
        esac
    done

    if [[ -z $command ]]; then
        candidates=(@ROOT_CANDIDATES@)
        compadd -- "${candidates[@]}"
        return
    fi

    if [[ $command == exec ]]; then
        for ((index = 2; index < CURRENT; index++)); do
            if [[ ${words[index]} == -- ]]; then
                separator=$index
                break
            fi
        done
        if (( separator > 0 )); then
            words=("${(@)words[$((separator + 1)),-1]}")
            (( CURRENT -= separator ))
            _normal
            return
        fi
    fi

    if [[ $command == completion ]]; then
        candidates=(@SHELL_CANDIDATES@)
        compadd -- "${candidates[@]}"
        return
    fi

    case "$previous" in
        -t|--topo)
            _files
            return
            ;;
        -n|--name)
            candidates=("${(@f)$(
                command "${words[1]}" __complete names "$current" 2>/dev/null
            )}")
            (( ${#candidates} )) && compadd -- "${candidates[@]}"
            return
            ;;
        --node)
            if [[ $command == exec ]]; then
                candidates=("${(@f)$(
                    command "${words[1]}" __complete nodes \
                        "$((CURRENT - 1))" "${words[@]}" 2>/dev/null
                )}")
                (( ${#candidates} )) && compadd -- "${candidates[@]}"
            fi
            return
            ;;
        --format)
            case "$command" in
                inspect) candidates=(@INSPECT_FORMATS@) ;;
                graph) candidates=(@GRAPH_FORMATS@) ;;
            esac
            compadd -- "${candidates[@]}"
            return
            ;;
    esac

    case "$command" in
@ZSH_OPTION_CASES@
    esac
    compadd -- "${candidates[@]}"
}

if (( ! $+functions[compdef] )); then
    autoload -Uz compinit
    compinit
fi
compdef _nslab_completion nslab
"""


def _shell_words(values: Sequence[str]) -> str:
    return " ".join(values)


def _command_pattern() -> str:
    return "|".join(PUBLIC_COMMANDS)


def _bash_option_cases() -> str:
    return "\n".join(
        f"        {command}) candidates=({_shell_words(options)}) ;;"
        for command, options in _COMMAND_OPTIONS.items()
    )


def _zsh_option_cases() -> str:
    return "\n".join(
        f"        {command}) candidates=({_shell_words(options)}) ;;"
        for command, options in _COMMAND_OPTIONS.items()
    )


def render_completion_script(shell: str) -> str:
    replacements = {
        "@COMMAND_PATTERN@": _command_pattern(),
        "@ROOT_CANDIDATES@": _shell_words((*PUBLIC_COMMANDS, "-h", "--help", "--debug")),
        "@SHELL_CANDIDATES@": _shell_words(COMPLETION_SHELLS),
        "@INSPECT_FORMATS@": _shell_words(INSPECT_FORMATS),
        "@GRAPH_FORMATS@": _shell_words(GRAPH_FORMATS),
        "@BASH_OPTION_CASES@": _bash_option_cases(),
        "@ZSH_OPTION_CASES@": _zsh_option_cases(),
    }
    if shell == "bash":
        script = _BASH_TEMPLATE
    elif shell == "zsh":
        script = _ZSH_TEMPLATE
    else:
        raise ValueError(f"unsupported completion shell: {shell}")
    for marker, value in replacements.items():
        script = script.replace(marker, value)
    return script


def _option_value(words: Sequence[str], cursor_index: int, *options: str) -> str | None:
    value: str | None = None
    index = 1
    long_options = tuple(option for option in options if option.startswith("--"))
    while index < cursor_index:
        word = words[index]
        if word in options:
            if index + 1 < cursor_index:
                value = words[index + 1]
                index += 2
                continue
            return value
        for option in long_options:
            prefix = f"{option}="
            if word.startswith(prefix):
                value = word[len(prefix) :]
                break
        index += 1
    return value


def _deployment_names(state_root: Path, prefix: str) -> tuple[str, ...]:
    names: list[str] = []
    for path in state_root.iterdir():
        if (
            path.suffix != ".json"
            or NAME_PATTERN.fullmatch(path.stem) is None
            or not path.is_file()
        ):
            continue
        names.append(path.stem)
    return tuple(name for name in sorted(set(names)) if name.startswith(prefix))


def _selected_manifest(
    words: Sequence[str],
    cursor_index: int,
    *,
    cwd: Path,
    state_root: Path,
) -> Manifest | None:
    topology = _option_value(words, cursor_index, "-t", "--topo")
    name = _option_value(words, cursor_index, "-n", "--name")
    if topology is not None:
        path = Path(topology).expanduser()
        if not path.is_absolute():
            path = cwd / path
        return load_manifest(path)
    if name is not None:
        snapshot = StateStore(state_root).load(name)
        if snapshot is None:
            return None
        return Manifest.model_validate(dict(snapshot.manifest))
    return load_manifest(cwd / "nslab.yaml")


def _node_names(
    words: Sequence[str],
    cursor_index: int,
    *,
    cwd: Path,
    state_root: Path,
) -> tuple[str, ...]:
    if cursor_index < 1 or cursor_index >= len(words):
        return ()
    manifest = _selected_manifest(words, cursor_index, cwd=cwd, state_root=state_root)
    if manifest is None:
        return ()
    prefix = words[cursor_index]
    return tuple(name for name in sorted(manifest.topology.nodes) if name.startswith(prefix))


def hidden_completion_candidates(
    arguments: Sequence[str],
    *,
    cwd: Path,
    state_root: Path,
) -> tuple[str, ...]:
    try:
        if not arguments:
            return ()
        if arguments[0] == "names":
            prefix = arguments[1] if len(arguments) > 1 else ""
            return _deployment_names(state_root, prefix)
        if arguments[0] == "nodes" and len(arguments) >= 3:
            cursor_index = int(arguments[1])
            return _node_names(
                arguments[2:],
                cursor_index,
                cwd=cwd,
                state_root=state_root,
            )
    except Exception:
        return ()
    return ()
