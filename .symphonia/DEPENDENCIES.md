# Third-party dependencies

**TLDR: the workflow depends on three skills it does not ship. `install.py` checks for them and reports what is missing; it never installs them.**

| Skill | Why the workflow needs it |
|---|---|
| `grilling` | Drives the brainstorming of the "new idea" intake path, paired with `/wayfinder`. |
| `code-review` | The review loop every Implementation Ticket goes through before its merge gate. |
| `tdd` | Drives risky behavior changes test-first during implementation. |

`simplify` was left open in GRE-168 and is deliberately not declared.
