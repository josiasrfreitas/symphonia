"""The `map` verbs — one module per verb, discovered by convention.

`map.discover()` imports every module in this package whose name does not
start with `_` and registers it under that name. There is no hand-written
list: adding `frontier.py` here is what makes `map frontier` exist.

A verb module exposes exactly two names:

    SPEC = {
        "name": "frontier",                      # matches the module name
        "help": "the ordered set of tickets that can be worked now",
        "required": ("map",),                    # parameter names, no dashes
        "example": "map frontier --map SYM-8",
    }

    def run(params: dict, *, tracker) -> str:
        ...

Every name in `required` takes a value, and `map` guarantees each one
arrives as a non-empty string. A parameter that is a bare `--flag` stays
out of `required`: `check` refuses `--key` with nothing after it rather
than passing `True` where the verb expected `SYM-8`.

`params` is what the caller passed, already checked against `required` —
a verb never re-validates presence, and never asks the user for a missing
value: `map` refuses first, in the one Context Injection format
(`injection.render`).

`tracker` is a zero-argument factory, not a tracker. Call it only if the
verb talks to the tracker, so a read-only or refusing call keeps working
without `LINEAR_API_KEY` set. Calling it twice returns the same object.

Whatever `run` returns is printed verbatim: it is the verb's Context
Injection, composed by this script and read by an agent. To refuse from
inside a verb, raise `injection.Refused` — every other exception keeps its
traceback, because a bug must not read as a polite refusal.

The eight verbs, in the order a route uses them: `new` opens a map,
`ticket` puts a question on it or rewires one already there, `frontier`
says what can be worked now, `claim` takes one and hands back the ticket
plus the map in low resolution, `resolve` answers it and indexes the
decision, `validate` says whether the map is finished and what is
malformed, `graph` draws it, and `brief` writes the briefing an agent
starts from.

`_shared.py` is not a verb — `discover()` skips it — it is where the
seven bureaucracy verbs keep the body formats they all write and read.
"""
