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

No verb exists yet: SYM-10 builds the dispatcher, SYM-11 and SYM-12 fill
this package.
"""
