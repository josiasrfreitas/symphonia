#!/usr/bin/env python3
"""The `map` dispatcher and its stateless guided mode.

TLDR: `map <verb> --key value ...`. Verbs are discovered from `verbs/`,
one module per verb (see `verbs/__init__.py` for the contract). Every way
a call can fail before a verb runs — no verb, unknown verb, a missing
required parameter — comes back as the one Context Injection refusal
format (`injection`), never as a traceback and never as an interactive
prompt.

Stateless guided mode is that last part, and it is the whole of it: the
direct mode and the guided mode run the *same* validation, and where a
conversational tool would ask a question and remember the answer, this one
answers with what is missing and forgets. There is no session to resume,
so a caller that dies mid-way loses nothing, and the reply is equally
readable by a human and by the agent that will retry the call.

`main()` is the only place a `Refused` becomes output and an exit code;
anything else propagates, because a bug must stay visible as a bug.
"""
from __future__ import annotations

import difflib
import importlib
import json
import pkgutil
import sys
from typing import Any, Callable

from injection import INCOMPLETE, REFUSED, Refusal, Refused, as_dict, render

REFUSED_EXIT = 2
"""Exit code for a refusal. Distinct from 1, which stays what an
unhandled error exits with — a caller can tell "you called it wrong" from
"it broke" without reading the text."""


def discover(package: str = "verbs") -> dict[str, Any]:
    """Every verb module in `package`, keyed by module name. Names
    starting with `_` are helpers, not verbs."""

    module = importlib.import_module(package)
    return {
        info.name: importlib.import_module(f"{package}.{info.name}")
        for info in pkgutil.iter_modules(module.__path__)
        if not info.name.startswith("_")
    }


def parse(argv: list[str]) -> tuple[str, dict]:
    """`["frontier", "--map", "SYM-8", "--verbose"]` becomes
    `("frontier", {"map": "SYM-8", "verbose": True})`. A `--flag` with no
    value that follows is `True`; anything else is the previous flag's
    value. Pure — no verb is imported here."""

    verb = argv[0] if argv and not argv[0].startswith("-") else ""
    params: dict = {}
    rest = argv[1:] if verb else argv
    key = None
    for token in rest:
        if token.startswith("--"):
            key = token[2:]
            params[key] = True
        elif key is not None:
            params[key] = token
            key = None
        else:
            # Nothing is missing here — the shape is wrong, and more of
            # the same does not fix it. That is REFUSED, not INCOMPLETE.
            raise Refused(Refusal(
                blocked=f"the value {token!r} came before any --parameter",
                accepted="values follow the parameter they belong to",
                example="map frontier --map SYM-8",
                kind=REFUSED,
            ))
    return verb, params


def _verb_list(registry: dict) -> str:
    return ", ".join(sorted(registry)) if registry else ""


def _example(registry: dict, prefer: str = "") -> str:
    """A call that really works, taken from a verb's own `SPEC` — the
    field promises "one concrete call that would work", and `map <verb>`
    with no parameters is a call this same tool would refuse."""

    if not registry:
        return "map <verb> --key value — none exists yet in this build"
    name = prefer if prefer in registry else sorted(registry)[0]
    return registry[name].SPEC["example"]


def resolve(verb: str, registry: dict) -> Any:
    """The module for `verb`, or a `Refused` that says how to get one."""

    known = _verb_list(registry)
    if not verb:
        raise Refused(Refusal(
            blocked="no verb was given",
            accepted=f"one of: {known}" if known else "no verbs are registered yet",
            example=_example(registry),
            kind=INCOMPLETE,
        ))
    if verb in registry:
        return registry[verb]
    near = difflib.get_close_matches(verb, sorted(registry), n=1)
    raise Refused(Refusal(
        blocked=f"{verb!r} is not a verb of this tool",
        accepted=f"one of: {known}" if known else "no verbs are registered yet",
        example=_example(registry, near[0] if near else ""),
        kind=REFUSED,
    ))


def check(spec: dict, params: dict) -> None:
    """The stateless guided mode: a call missing a required parameter is
    answered with the names it is missing, not with a question. Three
    gaps, three answers — absent, valueless, empty — because a refusal
    that names the wrong gap teaches the wrong fix.

    Only `required` names are checked here, and a required name always
    takes a value: a boolean flag belongs outside `required`, by the
    contract in `verbs/__init__.py`."""

    required = tuple(spec.get("required", ()))
    accepted = ("every required parameter in the same call, each with a value: "
                + ", ".join(f"--{name} <value>" for name in required))

    def refuse(blocked: str) -> None:
        raise Refused(Refusal(
            blocked=blocked,
            accepted=accepted,
            example=spec["example"],
            kind=INCOMPLETE,
        ))

    absent = [name for name in required if name not in params]
    if absent:
        refuse(f"{spec['name']} was called without: {', '.join(absent)}")
    # `parse` writes True for a `--key` with nothing after it. Handing that
    # to a verb that expected `SYM-8` is guessing, which this tool does not do.
    valueless = [name for name in required if not isinstance(params[name], str)]
    if valueless:
        refuse(f"{spec['name']} got no value after: "
               + ", ".join(f"--{name}" for name in valueless))
    empty = [name for name in required if not params[name].strip()]
    if empty:
        refuse(f"{spec['name']} was given an empty value for: "
               + ", ".join(f"--{name}" for name in empty))


def _tracker_factory() -> Callable[[], Any]:
    """A zero-argument factory that builds the tracker at most once, and
    only if a verb asks for it — same shape as `gate.run`, and for the
    same reason: a call that never touches Linear must work without
    `LINEAR_API_KEY`."""

    built: list = []

    def resolved():
        if not built:
            import linear  # imported late: the module is only needed here

            built.append(linear.LinearTracker())
        return built[0]

    return resolved


def main(
    argv: list[str] | None = None,
    *,
    registry: dict | None = None,
    tracker: Callable[[], Any] | None = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    argv = [token for token in argv if token != "--json"]
    try:
        verb, params = parse(argv)
        module = resolve(verb, registry if registry is not None else discover())
        check(module.SPEC, params)
        print(module.run(params, tracker=tracker or _tracker_factory()))
        return 0
    except Refused as refusal:
        text = (
            json.dumps(as_dict(refusal.refusal), ensure_ascii=False)
            if as_json else render(refusal.refusal)
        )
        print(text, file=sys.stderr)
        return REFUSED_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
