from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from pyroute2.netlink.rtnl.tcmsg.common import red_eval_ewma, red_eval_idle_damping

from nslab.errors import NslabError
from nslab.planner import SimpleQdiscPlan

SIMPLE_QDISC_KINDS = frozenset(
    {"pfifo", "bfifo", "pfifo_fast", "prio", "sfq", "fq", "codel", "red", "pie", "fq_pie"}
)
_TIME_OPTIONS = {"target": "target_ms", "interval": "interval_ms", "tupdate": "tupdate_ms"}
_INTEGER_OPTIONS = frozenset(
    {
        "limit",
        "flows",
        "quantum",
        "perturb",
        "min",
        "max",
        "alpha",
        "beta",
        "ewma",
        "Scell_log",
        "Plog",
    }
)
_BOOLEAN_OPTIONS = {
    "codel": ("ecn",),
    "pie": ("ecn", "bytemode"),
    "fq_pie": ("ecn", "bytemode"),
    "red": ("ecn", "harddrop", "adaptive", "nodrop"),
}
_TC_H_ROOT = 0xFFFFFFFF


def _unsupported(namespace: str, reason: str) -> NslabError:
    return NslabError(
        code="INVENTORY_UNSUPPORTED",
        message=f"unsupported tc qdisc in network inventory: {namespace}",
        details={"operation": "inventory", "resource": namespace, "reason": reason},
    )


def _handle(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    if value == "root":
        return _TC_H_ROOT
    parts = value.split(":")
    if len(parts) != 2:
        return None
    try:
        major, minor = (int(part or "0", 16) for part in parts)
    except ValueError:
        return None
    if not (0 <= major <= 0xFFFF and 0 <= minor <= 0xFFFF):
        return None
    return (major << 16) | minor


def decode_simple_qdisc(
    kind: str,
    handle: int,
    parent: int,
    records: Sequence[Mapping[str, object]],
    namespace: str,
) -> SimpleQdiscPlan:
    """Join tc JSON to a Netlink qdisc by identity, never by requested options."""
    matches = [
        record
        for record in records
        if record.get("kind") == kind
        and _handle(record.get("handle")) == handle
        and (
            (record.get("root") is True and record.get("parent") is None)
            if parent == _TC_H_ROOT
            else (not record.get("root") and _handle(record.get("parent")) == parent)
        )
    ]
    if kind not in SIMPLE_QDISC_KINDS or len(matches) != 1:
        raise _unsupported(namespace, "tc_identity")
    raw = matches[0].get("options")
    if not isinstance(raw, dict):
        raise _unsupported(namespace, "tc_options")
    options: dict[str, object] = dict(raw)
    for name in _INTEGER_OPTIONS & options.keys():
        value = options[name]
        if type(value) is not int or value < 0:
            raise _unsupported(namespace, "tc_parameters")
    for name, alias in _TIME_OPTIONS.items():
        if name not in options:
            continue
        value = options.pop(name)
        if type(value) is not int or value <= 0:
            raise _unsupported(namespace, "tc_parameters")
        milliseconds = round(value / 1000)
        if milliseconds <= 0 or abs(value - milliseconds * 1000) > 1:
            raise _unsupported(namespace, f"{name}_precision")
        options[alias] = milliseconds
    for name in _BOOLEAN_OPTIONS.get(kind, ()):
        # tc omits disabled CoDel/PIE flags, but emits explicit RED booleans.
        value = options.get(name, False)
        if type(value) is not bool:
            raise _unsupported(namespace, "tc_parameters")
        options[name] = value
    if "probability" in options:
        probability = options["probability"]
        if (
            not isinstance(probability, (int, float))
            or isinstance(probability, bool)
            or not math.isfinite(probability)
            or not 0 <= probability <= 1
        ):
            raise _unsupported(namespace, "tc_parameters")
    return SimpleQdiscPlan(kind, options)


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("expected integer qdisc option")
    return value


def _red_effective_options(options: Mapping[str, object]) -> dict[str, object]:
    """Mirror tc's RED defaults and compare only kernel-observable parameters."""
    limit = _integer(options["limit"])
    avpkt = _integer(options["avpkt"])
    minimum = _integer(options.get("min", 0))
    maximum = _integer(options.get("max", 0)) or (minimum * 3 if minimum else limit // 4)
    minimum = minimum or maximum // 3
    burst = _integer(options.get("burst", 0)) or (2 * minimum + maximum) // (3 * avpkt)
    a = burst + 1 - minimum / avpkt
    if a < 1:
        raise ValueError("invalid RED burst")
    # pyroute2's helper skips Wlog=1; tc tests it before halving W.
    ewma = 1 if a <= (1 - 0.5**burst) / 0.5 else red_eval_ewma(minimum, burst, avpkt)
    if not 1 <= ewma < 32:
        raise ValueError("invalid RED EWMA")
    scell_log = red_eval_idle_damping(ewma, avpkt, 1_250_000)[0]
    if not 0 <= scell_log < 32:
        raise ValueError("invalid RED idle damping")
    return {
        **{name: value for name, value in options.items() if name not in {"avpkt", "burst"}},
        "min": minimum,
        "max": maximum,
        "ewma": ewma,
        "Scell_log": scell_log,
        "probability": options.get("probability", 0.02),
        "ecn": options.get("ecn", False),
        "harddrop": False,
        "adaptive": False,
        "nodrop": False,
    }


def simple_qdisc_matches(desired: SimpleQdiscPlan, observed: SimpleQdiscPlan) -> bool:
    if desired.kind != observed.kind:
        return False
    if desired == observed:
        return True
    expected = desired.options
    if desired.kind == "red":
        try:
            expected = _red_effective_options(expected)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return False
    for name, value in expected.items():
        if name not in observed.options:
            return False
        actual = observed.options[name]
        if desired.kind == "red" and name == "probability":
            # tc's JSON printer uses six significant digits; max_P is a u32.
            if not isinstance(actual, (int, float)) or not isinstance(value, (int, float)):
                return False
            if not math.isclose(value, actual, rel_tol=5e-6, abs_tol=1 / 2**32):
                return False
        elif value != actual:
            return False
    return True
