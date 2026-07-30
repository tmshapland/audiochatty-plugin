#!/usr/bin/env python3
"""`audiochatty run` — Claude Code, wrapped, with a return path.

`wrapper_return_path_plan.md` Phase 1 · W1, W3, W5, W6, W8, W9.

This process is the replacement for `channel/server.ts`. The old return path was a Claude
Code *plugin*, and loading it needed `--dangerously-load-development-channels`, which
prints a warning about untrusted development content. 👤 rejected asking users to do that
(2026-07-29), so the delivery mechanism moved out of the session and in front of it:

    you  ─keystrokes─▶  this process  ─▶  a pty  ─▶  claude
                             ▲
                             └── an instruction you spoke into audiochatty, typed in
                                 for you as if you had typed it at the keyboard

Claude Code cannot tell. There is no plugin loaded, no flag, no channel — from the
session's point of view somebody typed something.

**Stdlib only**, like the rest of `scripts/`. The old Bun-based channel broke the plugin's
"read the code, then install it — no build step" promise; this puts it back.

## The local API — frozen (Phase 1's first obligation)

Phase 3 (`scripts/audiochat.py`) is written against *this section and nothing else*. It
does not import this package and must never need to.

**How a slash command finds its wrapper (W3).** Two environment variables are set in the
environment `claude` inherits, so everything the session runs can read them:

    AUDIOCHATTY_WRAPPER_PORT   the loopback port below
    AUDIOCHATTY_WRAPPER_PID    this process's pid, i.e. which rendezvous file is ours

That is the whole of the correlation mechanism. It replaces ~200 lines of process-table
inspection in the old design (`audiochat.py:344-541`), which tried to answer "which
session am I?" by reading `ps`.

**The rendezvous file**, `~/.audiochatty/wrappers/<pid>.json`, mode 0600, written
atomically (`AUDIOCHATTY_HOME` overrides `~/.audiochatty`, as everywhere else here):

    {
      "version": 1,
      "kind": "wrapper",              # tells a reader this is not an old channel file
      "pid": 4242,                    # the wrapper; the file name matches
      "child_pid": 4243,              # the `claude` it started
      "port": 51837,                  # 127.0.0.1 only, ephemeral
      "started_at": "2026-07-29T...Z",
      "expected_session_id": "…uuid…" | null,   # see below
      "generation": 0,
      "bound": false,
      "verified": false,
      "claude_session_id": null,
      "agent_session_id": null,
      "session_name": null,
      "backend_url": null,
      "bound_at": null,
      "verified_at": null
    }

**The device token is deliberately not in here.** It lives in `credentials.json` and has
no business being copied into a second file. Everything above is either public (a port, a
pid) or already known to whoever can read the directory.

`expected_session_id` is the safety check W3 asks for. The wrapper mints a uuid and starts
`claude --session-id <uuid>`, so it knows its child's session id *before* anything binds,
and `/bind` can refuse a session id that isn't its own. Without it, this leaks: a session
started by this wrapper runs a Bash tool call, someone types plain `claude` inside it, that
inner session inherits `AUDIOCHATTY_WRAPPER_PORT`, and `/audiochatty-connect` there would
bind *this* wrapper to *that* session's id — pointing your spoken instructions at the wrong
terminal. It is `null` only when the user's own arguments already decide the session
(`--session-id`, `--resume`, `--continue`), in which case `/bind` pins the first session id
it is given instead.

**The endpoints.** All on `127.0.0.1:<port>`, all JSON, all requiring the device token from
`credentials.json` — the token is what stops a stray local process pointing this wrapper at
a session it does not own.

    POST /bind      {agent_session_id, claude_session_id, backend_url, token,
                     session_name?, verified_reported?}
                    200 {"status": "bound"|"rebound", pid, claude_session_id,
                         agent_session_id, verified}
                    400 invalid_json | missing_fields
                    403 token_mismatch
                    409 session_mismatch   — not the session this wrapper started (W3)
                    409 already_bound      — bound to a different session already

    POST /unbind    {token?}
                    200 {"status": "unbound", claude_session_id}
                    403 token_mismatch

    GET  /status    200 {pid, child_pid, port, bound, verified, generation,
                         claude_session_id, agent_session_id, expected_session_id,
                         pending_injections, polling}
                    Never mentions the token. Always answers immediately.

    POST /inject    {token, text, message_id?}
                    202 {"status": "queued", pending}
                    400 invalid_json | missing_fields
                    403 token_mismatch
                    409 not_bound

`/inject` is a **deviation from the plan's three-endpoint list**, and worth the extra
surface for two reasons: Phase 1 has to prove "injected text actually reaches the child"
and nothing else can trigger an injection until Phase 2's poller exists, and Phase 8's
Stop-hook fast path needs exactly this door. It is the sharp edge in this whole design —
anything that can reach this port and read `credentials.json` can type anything into your
terminal — which is why it is token-gated, refuses while unbound, and is written up in
`wrapper/README.md` in those words.

Two fields on that list are Phase 2's, and both are additive — Phase 3 can ignore either
and still be correct. `verified_reported` on `/bind` is how `/audiochatty-connect` says it
has already told the backend this session is reachable; leaving it out means the poll loop
keeps trying until the backend confirms, which is the safe default (`poller.Poller.start`).
`polling` on `/status` answers the one question the rendezvous file cannot: bound, but is
anything actually asking?

## What runs, in what order, and why the order is load-bearing

    1. bind the loopback socket        → we need its port before the child exists
    2. spawn `claude` on a pty         → still single-threaded: see below
    3. start the control server thread → now threads are allowed
    4. run the proxy loop              → until the child exits
    5. exit with the child's code      → `audiochatty run` behaves like `claude` in a script

The poll loop is not in that list because it is not started here: nothing polls until a
session binds, and until then this process makes no network calls at all. `/bind` starts
it, `/unbind` stops it, and step 5 closes it — see `poller.py`.

Steps 1-3 are in that order because step 2 forks, and `subprocess`'s `preexec_fn` runs
Python between the fork and the exec. In a process that already has threads, that is a
documented way to deadlock on a lock some other thread was holding. So the socket is bound
early (which yields the port without serving anything) and the server thread starts late.
Moving the `ControlServer.serve()` call above `spawn()` would reintroduce that.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

# Importable both as `python3 -m wrapper` and as a bare path — `wrapper/audiochatty` runs
# the second form, and an alias in someone's shell profile is the whole install story.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from wrapper import store  # noqa: E402
from wrapper.control import ControlServer  # noqa: E402
from wrapper.inject import DEFAULT_QUIET_PERIOD, Injector  # noqa: E402
from wrapper.poller import Poller  # noqa: E402
from wrapper.pty_proxy import PtyProxy, SpawnFailed  # noqa: E402

# Arguments that mean "the session id is already decided, don't mint one". `--session-id`
# is the direct case; the others resume an existing session, which has an id we cannot
# know or choose.
SESSION_DECIDING_ARGS = ("--session-id", "--resume", "-r", "--continue", "-c", "--from-pr")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audiochatty",
        description="Start Claude Code with an audiochatty return path attached.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser(
        "run",
        help="Start `claude`, wrapped. Anything after `--` is passed straight to it.",
    )
    run.add_argument(
        "--claude-bin",
        default=os.environ.get("AUDIOCHATTY_CLAUDE_BIN", "claude"),
        help="The program to wrap. Exists for the test suite; you will not need it.",
    )
    run.add_argument(
        "--quiet-period",
        type=float,
        default=float(os.environ.get("AUDIOCHATTY_QUIET_PERIOD", DEFAULT_QUIET_PERIOD)),
        help=(
            "Seconds of no typing before a spoken instruction is typed in (W6). "
            f"Default {DEFAULT_QUIET_PERIOD}."
        ),
    )
    run.add_argument(
        "--verbose",
        action="store_true",
        help="Print the port and rendezvous path before starting. Off by default: a "
        "wrapped session should look exactly like an unwrapped one.",
    )
    run.add_argument(
        "claude_args",
        nargs=argparse.REMAINDER,
        help="Passed to `claude` unchanged.",
    )
    return parser


def cmd_run(args: argparse.Namespace) -> int:
    claude_args = [a for a in args.claude_args if a != "--"]

    # W13's hook-free half: we choose the session id, so we know it before the session
    # exists. See the `expected_session_id` note in the module docstring for what it buys.
    expected_session_id: str | None = None
    if not any(a in SESSION_DECIDING_ARGS for a in claude_args):
        expected_session_id = str(uuid.uuid4())
        claude_args = ["--session-id", expected_session_id, *claude_args]

    store.prune_stale()

    # 1. The socket, for its port. Nothing is served until step 3.
    control = ControlServer()

    child_env = dict(os.environ)
    child_env["AUDIOCHATTY_WRAPPER_PORT"] = str(control.port)
    child_env["AUDIOCHATTY_WRAPPER_PID"] = str(os.getpid())

    injector = Injector(quiet_period=args.quiet_period)
    # Constructed unconditionally, started by nothing: it makes no network call until a
    # session binds. That is the "costs nothing until connected" property, and it is the
    # reason this can be a plain object rather than an option.
    poller = Poller(injector)
    proxy = PtyProxy(
        [args.claude_bin, *claude_args],
        env=child_env,
        injector=injector,
    )

    if args.verbose:
        print(
            f"audiochatty: port {control.port} · rendezvous "
            f"{store.rendezvous_path()} · session {expected_session_id or '(inherited)'}",
            file=sys.stderr,
        )

    # 2. The child, while this process is still single-threaded.
    try:
        proxy.spawn()
    except SpawnFailed as err:
        control.close()
        print(f"audiochatty: {err}", file=sys.stderr)
        return 127

    # `spawn` has put the real terminal into raw mode, so from here on every exit path has to
    # put it back — including one that never reaches `run()`.
    try:
        state = store.WrapperState(
            pid=os.getpid(),
            port=control.port,
            child_pid=proxy.child_pid,
            expected_session_id=expected_session_id,
            injector=injector,
            poller=poller,
        )
        # 3. Threads are allowed from here on.
        control.serve(state)
    except Exception:
        proxy.restore_terminal()
        control.close()
        raise

    try:
        return proxy.run()
    finally:
        # The control port first, so nothing can bind while we are shutting down; then the
        # poller, whose last act is to record anything the injector typed but had not yet
        # reported (W9); then the rendezvous file, because a file for a process that has
        # exited is worse than no file at all.
        control.close()
        poller.close()
        state.cleanup()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
