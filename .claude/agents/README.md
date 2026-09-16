# The agents that work on this codebase

Two kinds of agent exist here, and they are not the same thing.

**Runtime agents** live in `agents/` and run inside the site on every
reply: the router (which channel is answering well right now), the
verifier (a code answer is run before it is trusted) and the evaluator
(the offline harness that measures accuracy per model). See
`agents/__init__.py`.

**These files** define agents for Claude Code to run on the repository.
Each is a specialist with a narrow brief and the exact commands it
should use. Ask for one by name ("use the perf engineer on the boot
sequence", "have the ui designer look at the settings modal") or let
Claude pick the one whose description fits.

| agent                | does                                                       | changes code? |
|----------------------|------------------------------------------------------------|---------------|
| `perf-engineer`      | finds where time goes and removes it                       | yes           |
| `accuracy-evaluator` | runs the eval harness, moves prompts/routes toward what measures best | yes |
| `ui-designer`        | screenshots the app, refines it against the design tokens  | yes           |
| `qa-verifier`        | runs every check, reads the diff, reports what would break | no            |
| `security-reviewer`  | reads the trust boundaries, reports                        | no            |
| `release-manager`    | commits in the repo's voice, deploys, verifies the build   | commits only  |

Every agent that changes code runs the full check suite before it
reports done. Every agent obeys `.claude/settings.json`: it never reads
`.env`, the database, or keys.
