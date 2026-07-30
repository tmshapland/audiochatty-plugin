#!/usr/bin/env python3
"""The audiochatty plugin's whole client — shared library and CLI in one file.

`coding_agent_build_plan.md` Phase 5 · `coding_agent_summary_plan.md` §5.

**Stdlib only.** `urllib` and `json`, no pip, no venv, no build step. That constraint is
what makes "read the repo, then install it" an honest offer (§5), and it is easy to break
with one convenient import. There is nothing to add here that is worth a dependency.

Four subcommands, one per slash command, plus the three hook scripts that import this
module:

    login       the RFC 8628 device flow — mint a code, then redeem it
    connect     register this Claude Code session under a name
    status      local-only: is this machine paired, is this session registered
    disconnect  retire this session

**`connect` is no longer a step in the happy path** (`wrapper_return_path_plan.md` W13).
`audiochatty run` connects the session it starts, and `scripts/session_start_hook.py`
connects the ones it could not name up front. The subcommand stays for repair — retry,
rename, reconnect — and the protocol all three share is `connect_session` below.

**What is on disk, and why so little.** `~/.audiochatty/` (0700) holds a credentials file
(0600) with the device token, a `sessions/` directory of marker files, a `disconnected/`
directory of tombstones for sessions the user closed by hand, a `wrappers/` directory each
`audiochatty run` writes its rendezvous into, and — only while a pairing is in flight — a
`pending.json` holding the `device_code`. Nothing else.

**There is one long-lived process now, it is not this one, and it is not inside the
session.** The return path is `wrapper/` — the process `audiochatty run` is
(`wrapper_return_path_plan.md` W1). It started this `claude` on a pty and can type into it,
it connects itself once the session exists, and everything this file does about it is in
"the wrapper" section below. This script itself is still what it was — short-lived, one
job, then gone.

**The token is never printed and never taken as an argument.** Anything typed into a
Claude Code prompt lands in the session `.jsonl` and in the model's context, and anything
this script prints does too, because a slash command's output *is* the prompt. So the
long-lived token travels backend → this process → 0600 file and stops there. The only
secret that ever reaches a terminal is the `user_code`, which is short-lived, single-use,
and useless without a signed-in browser.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# The deployed backend. `AUDIOCHATTY_BACKEND_URL` or `--backend-url` beats it, and once a
# machine is paired the URL it paired against is remembered in the credentials file — a
# device token is only valid at the backend that minted it, so the two belong together.
DEFAULT_BACKEND_URL = "https://audiochat-backend.onrender.com"

# Interactive commands can afford to wait; a person is watching.
CLI_TIMEOUT = 10.0
# The hook path cannot. See `post_turn` and the circuit breaker below.
#
# Raised from 2.5s when the turn payload grew to carry the full transcript, but only to
# 4.0 — and the gap between those two numbers is the whole trade. A bigger body wants a
# longer timeout; the plan's hard requirement (§8, and rule 2 in `stop_hook.py`) is that
# a backend which is down or asleep never makes the terminal wait, and *that* wins. The
# tests hold the line at six seconds wall-clock for a hook against a dead backend, which
# is the number this has to fit under, not the body size.
#
# It fits comfortably because gzip did the work instead: a typical turn is ~16 KB on the
# wire and the largest measured in this repo is 48 KB, where the cost is the handshake
# and the round trip, not the bytes. A turn too large to push through this is lost rather
# than delivered late — the right way round, since the alternative is a terminal that
# hangs after every prompt.
HOOK_TIMEOUT = 4.0

# Request bodies at least this large are gzipped. Below it the compression is not worth
# the header: a session registration is a few hundred bytes.
GZIP_MIN_BYTES = 4_096

# How long `login` keeps polling in one invocation before handing the terminal back.
# Bounded on purpose: a slash command that does not return is a slash command that has
# hung, and the flow is resumable by running it again.
DEFAULT_LOGIN_WAIT = 45.0

USER_AGENT = "audiochatty-plugin/0.1.0"


# -- where state lives -----------------------------------------------------------------


def config_dir() -> Path:
    """`~/.audiochatty`, created 0700 on first use.

    `AUDIOCHATTY_HOME` overrides it. That exists for the test suite, which must never touch
    a real developer's credentials, and it is the reason every test in this repo can run
    on a machine that is already paired.
    """
    override = os.environ.get("AUDIOCHATTY_HOME")
    base = Path(override) if override else Path.home() / ".audiochatty"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    # An existing directory keeps its own mode, so tighten it rather than trust it.
    try:
        base.chmod(0o700)
    except OSError:
        pass
    return base


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


def pending_path() -> Path:
    return config_dir() / "pending.json"


def state_path() -> Path:
    return config_dir() / "state.json"


def marker_path(claude_session_id: str) -> Path:
    """The registration marker for one Claude Code session.

    The file name is the session id, so the Stop hook answers "is *this* session
    registered" with one `os.path.exists` and no parsing. That check is what keeps the
    other fourteen terminals open that day silent (§8), and it has to be cheap because it
    runs on every turn of every session on the machine.
    """
    sessions = config_dir() / "sessions"
    sessions.mkdir(mode=0o700, parents=True, exist_ok=True)
    return sessions / f"{_safe_filename(claude_session_id)}.json"


def _safe_filename(value: str) -> str:
    """A session id is a uuid from Claude Code, but it arrives as untrusted text on the
    hook's stdin, and it is about to become a path. Keep it to characters that cannot
    escape the directory."""
    return "".join(c for c in str(value) if c.isalnum() or c in "-_")[:128]


def read_json(path: Path) -> dict:
    """A missing or corrupt file reads as `{}`. Every caller treats "no state" and
    "unreadable state" the same way, and there is no state here worth crashing over."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_private_json(path: Path, data: dict) -> None:
    """Write 0600, atomically.

    `os.open` with the mode set is the point: creating the file world-readable and
    chmod-ing it afterwards leaves a window where the token is readable, and on a shared
    machine that window is the whole vulnerability. The temp file is created in the same
    directory so `os.replace` is atomic.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


# -- talking to the backend ------------------------------------------------------------


def backend_url(override: str | None = None) -> str:
    """Resolution order: the flag, the environment, the URL this machine paired against,
    then the shipped default. The stored value comes third rather than first so a
    `--backend-url` run against local Flask works on a machine already paired to prod."""
    if override:
        return override.rstrip("/")
    from_env = os.environ.get("AUDIOCHATTY_BACKEND_URL")
    if from_env:
        return from_env.rstrip("/")
    stored = read_json(credentials_path()).get("backend_url")
    if stored:
        return str(stored).rstrip("/")
    return DEFAULT_BACKEND_URL


class ApiError(Exception):
    """A response that came back but said no. `status` is the HTTP status and `payload`
    the decoded JSON body, because this API's 4xx bodies are the message — RFC 8628's
    `authorization_pending` arrives as a 400 with a body worth reading."""

    def __init__(self, status: int, payload: dict, message: str = ""):
        super().__init__(message or payload.get("error") or f"HTTP {status}")
        self.status = status
        self.payload = payload


class TransportError(Exception):
    """Nothing came back: DNS, connection refused, TLS, timeout. Distinct from `ApiError`
    because the caller's response differs — a hook drops the turn and trips the breaker,
    while a CLI command tells the user their backend is unreachable."""


def post(
    path: str,
    body: dict,
    *,
    token: str | None = None,
    base_url: str | None = None,
    timeout: float = CLI_TIMEOUT,
) -> dict:
    """One POST, JSON in and JSON out. Raises `ApiError` or `TransportError`.

    A 204 (which `/agent/session/end` returns for a session the backend has never heard
    of) decodes as `{}` rather than failing — an empty body is a valid answer here.

    Bodies over `GZIP_MIN_BYTES` are compressed. That is here for `/agent/turn`, which
    carries a whole turn's transcript: real turns compress to about a fifth, which is the
    difference between a hook that finishes inside its timeout and one that loses turns
    on a slow uplink. The backend decompresses on the way in (`app/__init__.py`).
    """
    url = f"{base_url or backend_url()}{path}"
    data = json.dumps(body).encode("utf-8")
    compressed = len(data) >= GZIP_MIN_BYTES
    if compressed:
        data = gzip.compress(data, 6)
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if compressed:
        request.add_header("Content-Encoding", "gzip")
    request.add_header("User-Agent", USER_AGENT)
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            pass
        raise ApiError(exc.code, _decode(raw)) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        raise TransportError(str(exc)) from exc

    return _decode(raw)


def _decode(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# -- the circuit breaker on the hook path ----------------------------------------------
#
# The plan's hard requirement is that a backend which is down or asleep must never make
# the terminal wait (§8). A short timeout alone does not deliver that: it makes *every*
# turn pay the timeout for as long as the outage lasts, and a sleeping Render service is
# an outage measured in hours.
#
# So a failed POST records a deadline, and until it passes the hook skips the network
# entirely. One turn pays the timeout; the next twenty pay nothing. The cost is that
# turns during the cooldown are dropped without being retried — which is already the
# contract on this path, since a turn nobody can deliver is a turn nobody hears.

BREAKER_COOLDOWN = 60.0


def breaker_is_open() -> bool:
    until = read_json(state_path()).get("backend_down_until")
    try:
        return float(until) > time.time()
    except (TypeError, ValueError):
        return False


def trip_breaker() -> None:
    _write_state_quietly({"backend_down_until": time.time() + BREAKER_COOLDOWN})


def reset_breaker() -> None:
    if read_json(state_path()):
        _write_state_quietly({})


def _write_state_quietly(state: dict) -> None:
    """Bookkeeping must never be the thing that breaks a turn. A read-only home
    directory, a full disk — the hook still exits 0 and the user never learns."""
    try:
        write_private_json(state_path(), state)
    except OSError:
        pass


# -- credentials and markers -----------------------------------------------------------


def load_credentials() -> dict:
    return read_json(credentials_path())


def device_token() -> str | None:
    token = load_credentials().get("token")
    return str(token) if token else None


def load_marker(claude_session_id: str) -> dict:
    if not claude_session_id:
        return {}
    return read_json(marker_path(claude_session_id))


def consume_skip_next_turn(claude_session_id: str, marker: dict) -> bool:
    """One-shot: True if this turn is the `/audiochatty-connect` turn itself.

    `connect` writes its marker during the slash command's `` !`…` `` preprocessing, so
    the session is already registered by the time that same turn ends — and the turn's
    only content is the model relaying "this session is now X in audiochatty". Delivering
    that as the first message tells the recipient something they just did themselves, in
    the voice of a session that has not done any work yet.

    So the flag is set at registration and cleared here, by the first Stop hook that sees
    it. A failed clear leaves the flag set and costs one further turn, which is the safe
    direction to fail in: the cost of a dropped turn is one missing blurb, the cost of a
    spurious one is a message the user did not want.
    """
    if not marker.get("skip_next_turn"):
        return False
    remaining = {key: value for key, value in marker.items() if key != "skip_next_turn"}
    try:
        write_private_json(marker_path(claude_session_id), remaining)
    except OSError:
        pass
    return True


# -- the disconnect tombstone (W13) ----------------------------------------------------
#
# `audiochatty run` connects its own session, and a `SessionStart` hook connects the ones
# it could not name up front (`--resume`). Both are automatic, and automatic reconnection
# is exactly wrong in one case: a session the user *deliberately* disconnected. `/clear`
# fires `SessionStart` again in the same session, so without a record of that decision a
# privacy action would silently undo itself a minute later.
#
# So `disconnect` leaves a tombstone, and only an explicit `/audiochatty-connect` clears
# it. A hook firing is not a decision; a typed command is. The rule this encodes: **never
# reconnect a session automatically after the user has closed it.**
#
# It lives in its own directory rather than beside the markers so it cannot be mistaken
# for one by anything globbing `sessions/*.json`.


def tombstone_path(claude_session_id: str) -> Path:
    directory = config_dir() / "disconnected"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / f"{_safe_filename(claude_session_id)}.json"


def session_was_disconnected(claude_session_id: str) -> bool:
    if not claude_session_id:
        return False
    return bool(read_json(tombstone_path(claude_session_id)))


def write_tombstone(claude_session_id: str) -> None:
    try:
        write_private_json(
            tombstone_path(claude_session_id),
            {"claude_session_id": claude_session_id, "disconnected_at": _now_iso()},
        )
    except OSError:
        # The disconnect itself has already happened locally — the marker is gone and the
        # wrapper is unbound. Failing to write this only costs stickiness across a later
        # `/clear`, which is not worth failing the command the user actually ran.
        pass


def clear_tombstone(claude_session_id: str) -> None:
    if not claude_session_id:
        return
    tombstone_path(claude_session_id).unlink(missing_ok=True)


# -- the wrapper (W1, W3) --------------------------------------------------------------
#
# The return path is a process that sits *in front of* this session rather than inside it.
# `audiochatty run` opened a pty, started this `claude` in it, and can type into it — see
# `wrapper/__main__.py`, whose docstring is the frozen API this section is written against
# and the only thing it is written against.
#
# **Finding it is one environment variable** (W3). The wrapper exports
# `AUDIOCHATTY_WRAPPER_PID` and `AUDIOCHATTY_WRAPPER_PORT` into the environment `claude`
# inherits, so everything the session runs — this command included — can read them. That
# replaces ~200 lines here that answered "which session am I?" out of `ps` output, with
# three independent signals and an ambiguity case. None of that can arise now: the answer
# is inherited rather than deduced, so there is exactly one candidate or none, and the old
# "found more than one" refusal has nothing left to describe.
#
# **What is still worth checking is that the candidate is really ours** (W3). A wrapped
# session can run a Bash tool call, and someone can type plain `claude` inside that — an
# inner session which inherits both variables and is *not* the one the wrapper started.
# Binding it would aim spoken instructions at the outer terminal. The wrapper minted its
# child's session id, so it publishes `expected_session_id` and this side compares against
# it. That is the one refusal left, and it is not the same answer as "no wrapper".

WRAPPER_TIMEOUT = 3.0

# How a session gets a return path. 👤 chose the bare command over an alias-only story
# (`wrapper_return_path_plan.md` Phase 0, 2026-07-29): "this is for developers, they can do
# commands." So this is printed flat wherever it appears, and the alias below is the install
# step that makes it exist rather than a second way to spell it.
RUN_COMMAND = "audiochatty run"

# The launcher shipped in this plugin, for the one line that turns `RUN_COMMAND` into a real
# command. Resolved from this file so it is correct wherever the plugin was installed, which
# a hardcoded `~/.claude/plugins/...` path would not be.
WRAPPER_LAUNCHER = Path(__file__).resolve().parent.parent / "wrapper" / "audiochatty"


def wrappers_dir() -> Path:
    """`~/.audiochatty/wrappers`, the directory the wrapper writes its rendezvous files to.

    Deliberately a second, smaller implementation of `wrapper/store.py`'s `wrappers_dir`,
    and the reason is the direction of the dependency: `store.py` imports *this* module for
    `write_private_json` and friends, so importing it back would be a cycle. Two four-line
    functions is the cheaper of those two problems, and this side only ever reads.
    """
    directory = config_dir() / "wrappers"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, belongs to somebody else.
        return True
    except OSError:
        return False
    return True


def read_wrappers() -> list[dict]:
    """Every live wrapper on this machine, from its rendezvous file.

    A file whose process is gone is skipped rather than deleted — the wrapper prunes those
    itself at startup, and a slash command that garbage-collects another process's state is
    a slash command that races with it.
    """
    found = []
    try:
        entries = sorted(wrappers_dir().glob("*.json"))
    except OSError:
        return []
    for path in entries:
        if not path.stem.isdigit():
            continue
        record = read_json(path)
        pid = record.get("pid")
        port = record.get("port")
        if not isinstance(pid, int) or not isinstance(port, int) or port <= 0:
            continue
        # A machine mid-upgrade could have both kinds of file around. `kind` is absent only
        # in a hand-written one, so a missing value reads as ours and a *different* value
        # does not.
        if str(record.get("kind") or "wrapper") != "wrapper":
            continue
        if not _pid_is_alive(pid):
            continue
        found.append(record)
    return found


def find_wrapper(claude_session_id: str) -> tuple[dict | None, str]:
    """The wrapper this session is running inside, or why there isn't one.

    Returns `(wrapper, "")`, `(None, "none")`, or `(None, "session_mismatch")`.

    `AUDIOCHATTY_WRAPPER_PID` names the rendezvous file and `AUDIOCHATTY_WRAPPER_PORT` is
    checked against what that file says, so a variable left behind by a wrapper that has
    since exited — and whose pid has since been reused by something else — resolves to
    nothing rather than to the wrong process.

    **`session_mismatch` is W3's refusal and it is a different answer from `none`.** It
    means a wrapper *was* found and it belongs to another session: the nested-`claude` case
    in this section's opening comment. Telling that user to run `audiochatty run` would be
    true and useless — they are already inside one.
    """
    pid_raw = os.environ.get("AUDIOCHATTY_WRAPPER_PID", "").strip()
    port_raw = os.environ.get("AUDIOCHATTY_WRAPPER_PORT", "").strip()
    if not port_raw.isdigit():
        _debug_wrapper("no AUDIOCHATTY_WRAPPER_PORT in the environment")
        return None, "none"
    port = int(port_raw)

    live = read_wrappers()
    if pid_raw.isdigit():
        pid = int(pid_raw)
        candidates = [w for w in live if w.get("pid") == pid and w.get("port") == port]
    else:
        # The pid is the better key, but the port alone still identifies a process: one
        # listener, one port. This is the path for a wrapper that exported only the port.
        candidates = [w for w in live if w.get("port") == port]

    if not candidates:
        _debug_wrapper(f"no live wrapper at pid={pid_raw or '?'} port={port}")
        return None, "none"

    wrapper = candidates[0]
    expected = str(wrapper.get("expected_session_id") or "")
    if expected and claude_session_id and expected != claude_session_id:
        _debug_wrapper(f"wrapper expects session {expected}, this is {claude_session_id}")
        return None, "session_mismatch"
    return wrapper, ""


def _debug_wrapper(message: str) -> None:
    """`AUDIOCHATTY_DEBUG=1` explains a refusal.

    Far less to explain than the old correlation needed, but "there is no wrapper" and
    "there is one and it isn't ours" are still worth telling apart on a machine where this
    is going wrong, and they are indistinguishable from the printed message alone.
    """
    if not os.environ.get("AUDIOCHATTY_DEBUG"):
        return
    print(f"[audiochatty] {message}", file=sys.stderr)


def _wrapper_base(wrapper: dict) -> str:
    return f"http://127.0.0.1:{int(wrapper['port'])}"


def bind_wrapper(wrapper: dict, *, agent_session_id: str, claude_session_id: str,
                 base: str, token: str, name: str) -> dict:
    """`POST /bind` on the wrapper's loopback port. Raises `ApiError`/`TransportError`."""
    return post(
        "/bind",
        {
            "agent_session_id": agent_session_id,
            "claude_session_id": claude_session_id,
            "backend_url": base,
            "token": token,
            "session_name": name,
        },
        base_url=_wrapper_base(wrapper),
        timeout=WRAPPER_TIMEOUT,
    )


def unbind_wrapper(wrapper: dict, token: str | None) -> bool:
    """`POST /unbind`, best effort. A wrapper that cannot be reached is one that has already
    exited, which is the same outcome by a different route."""
    try:
        post(
            "/unbind",
            {"token": token or ""},
            base_url=_wrapper_base(wrapper),
            timeout=WRAPPER_TIMEOUT,
        )
    except (ApiError, TransportError, KeyError, TypeError, ValueError):
        return False
    return True


def _print_relaunch(reason: str) -> None:
    """The one refusal left (W4). The old design had two — no channel, and a channel whose
    events were not honoured — and both meant "relaunch with the flag". There is no flag now,
    so there is one cause and one instruction.

    Rarely reached since W13: connecting is not a step anyone is told to take, so getting
    here means someone ran this command by hand in a session that has no return path. The
    instruction is the same, minus the "then run connect again" that is no longer true."""
    print(reason)
    print()
    print("Start Claude Code through audiochatty instead:")
    print()
    print(f"    {RUN_COMMAND}")
    print()
    print("That is the same Claude Code you already use — same terminal, no plugin to load,")
    print("no warning — and it connects the session itself, so there's nothing to run after")
    print("it.")
    print()
    print("If `audiochatty` isn't a command on this machine yet, that is one line in your")
    print("shell profile:")
    print()
    print(f'    alias audiochatty="{WRAPPER_LAUNCHER}"')


def _print_nested_session() -> None:
    """W3's refusal. Rare, but the generic advice is actively misleading here — this user
    did start a wrapped session, and is now in a second one nested inside it."""
    print("This isn't the session `audiochatty run` started. It looks like a plain `claude`")
    print("started from inside a wrapped one, which inherited the return path without owning")
    print("it.")
    print()
    print("Connecting it would type what you say into the outer terminal rather than this")
    print("one, so nothing was registered. The session you started with `audiochatty run`")
    print("is already connected — talk to that one.")


# -- login -----------------------------------------------------------------------------


def cmd_login(args: argparse.Namespace) -> int:
    """The device flow, in two invocations.

    **Why two.** A slash command's `` !`…` `` output is preprocessing: it is substituted
    into the prompt *after* the command exits, so nothing it prints is visible while it
    runs. A single invocation that minted a code and then polled would therefore show the
    user the code only once polling had already given up. So the first run mints and
    returns immediately, and the second — after the user has approved in the browser —
    collects the token. `pending.json` is what connects them.

    The second run still polls, for `--wait` seconds, so a user who approves promptly and
    runs the command again lands in the middle of a successful poll rather than having to
    time it. The `device_code` lives in that pending file and is never printed: it is the
    half of this flow that must not reach the transcript.
    """
    base = backend_url(args.backend_url)
    pending = read_json(pending_path())

    if args.reset:
        pending_path().unlink(missing_ok=True)
        pending = {}

    if _pending_is_live(pending) and pending.get("backend_url") == base:
        return _login_collect(pending, base, args.wait)
    return _login_start(base, args.label)


def _pending_is_live(pending: dict) -> bool:
    if not pending.get("device_code"):
        return False
    try:
        return float(pending.get("expires_at", 0)) > time.time()
    except (TypeError, ValueError):
        return False


def _login_start(base: str, label: str | None) -> int:
    """Mint a code and hand the terminal straight back."""
    existing = load_credentials()
    try:
        response = post(
            "/device/code",
            {"label": label or _default_label()},
            base_url=base,
            timeout=CLI_TIMEOUT,
        )
    except ApiError as exc:
        if exc.status == 429:
            print("Too many pairing attempts just now. Wait a few minutes and try again.")
            return 1
        print(f"Could not start pairing: {exc}")
        return 1
    except TransportError as exc:
        print(f"Could not reach audiochatty at {base} ({exc}).")
        return 1

    user_code = str(response.get("user_code") or "")
    verification_uri = str(response.get("verification_uri") or "")
    try:
        expires_in = float(response.get("expires_in") or 600)
    except (TypeError, ValueError):
        expires_in = 600.0

    write_private_json(
        pending_path(),
        {
            # The secret half of the flow. Written 0600, never printed.
            "device_code": response.get("device_code"),
            "user_code": user_code,
            "verification_uri": verification_uri,
            "interval": response.get("interval") or 5,
            "expires_at": time.time() + expires_in,
            "backend_url": base,
        },
    )

    if existing.get("token"):
        print("This machine is already paired. Finishing this will replace its token.")
        print()
    print(f"Open {verification_uri} and enter this code:")
    print()
    print(f"      {user_code}")
    print()
    print(f"It expires in {int(expires_in // 60)} minutes.")
    print("Once you've entered it, run /audiochatty-login again to finish.")
    return 0


def _login_collect(pending: dict, base: str, wait: float) -> int:
    """Poll `/device/token` until the code is redeemed, the budget runs out, or the
    backend says stop. RFC 8628 §3.5: `authorization_pending` and `slow_down` mean keep
    going, anything else means stop."""
    device_code = str(pending.get("device_code"))
    try:
        interval = max(float(pending.get("interval") or 5), 1.0)
    except (TypeError, ValueError):
        interval = 5.0

    deadline = time.time() + max(wait, 0.0)
    expires_at = float(pending.get("expires_at", 0))
    attempt = 0

    while True:
        attempt += 1
        try:
            response = post(
                "/device/token", {"device_code": device_code}, base_url=base, timeout=CLI_TIMEOUT
            )
        except TransportError as exc:
            print(f"Could not reach audiochatty at {base} ({exc}).")
            print("Your code is still valid — run /audiochatty-login again to retry.")
            return 1
        except ApiError as exc:
            error = str(exc.payload.get("error") or "")
            if error in ("authorization_pending", "slow_down"):
                if error == "slow_down":
                    # The backend told us we are early. Its stated interval is the floor.
                    interval = max(interval, float(exc.payload.get("interval") or interval)) + 1
                if time.time() + interval > min(deadline, expires_at):
                    return _login_still_waiting(pending, expires_at)
                time.sleep(interval)
                continue
            if error == "expired_token":
                pending_path().unlink(missing_ok=True)
                print("That code expired. Run /audiochatty-login to get a new one.")
                return 1
            if error == "access_denied":
                pending_path().unlink(missing_ok=True)
                print("That pairing was declined. Run /audiochatty-login to start over.")
                return 1
            # `invalid_grant` covers a code that was already redeemed as well as one the
            # backend has never heard of — indistinguishable from here, and the fix is
            # the same either way.
            pending_path().unlink(missing_ok=True)
            print("That pairing code is no longer valid. Run /audiochatty-login to start over.")
            return 1

        return _login_finish(response, base)


def _login_still_waiting(pending: dict, expires_at: float) -> int:
    remaining = max(int(expires_at - time.time()), 0)
    print(f"Still waiting for {pending.get('user_code')} to be approved.")
    print(f"Open {pending.get('verification_uri')} and enter it — {remaining // 60}m left.")
    print("Then run /audiochatty-login again.")
    return 0


def _login_finish(response: dict, base: str) -> int:
    """Store the token and say who we are. **The token is not printed here or anywhere
    else** — this function is the only place in the plugin that has ever seen it, and the
    file it writes is the only place it exists on this machine."""
    token = response.get("token")
    if not token:
        print("Pairing failed: the backend returned no token. Run /audiochatty-login again.")
        return 1

    write_private_json(
        credentials_path(),
        {
            "token": token,
            "backend_url": base,
            "device_id": response.get("device_id"),
            "workspace_id": response.get("workspace_id"),
            "workspace_name": response.get("workspace_name") or "",
            "profile_name": response.get("profile_name") or "",
            "label": response.get("label") or "",
            "paired_at": _now_iso(),
        },
    )
    pending_path().unlink(missing_ok=True)
    reset_breaker()

    workspace = response.get("workspace_name") or "audiochatty"
    who = response.get("profile_name")
    print(f"Linked to {workspace}" + (f" as {who}." if who else "."))
    print()
    # R12, and Phase 0's launch decision. Four surfaces describe this setup — two manifest
    # descriptions, the README, and here — and this is the only one whose words we fully
    # control and the only one the user is looking at when the step is actually due. So it
    # carries the command and the reason, not a pointer to somewhere else that carries them.
    print("One more step, and it's per session rather than per machine: a session can only")
    print("be talked to if you start Claude Code through audiochatty. Start it with:")
    print()
    print(f"    {RUN_COMMAND}")
    print()
    print("That's the whole of it — it connects the session for you, so there's nothing to")
    print("run afterwards. It's the same Claude Code you already use: same terminal, nothing")
    print("to load, no warning, with a return path attached. A session started with plain")
    print("`claude` has no return path, so there is nothing there to tell it what to do.")
    print()
    print("If `audiochatty` isn't a command on this machine yet, that is one line in your")
    print("shell profile:")
    print()
    print(f'    alias audiochatty="{WRAPPER_LAUNCHER}"')
    return 0


# -- connect ---------------------------------------------------------------------------
#
# **Three callers now, one protocol** (W13). `audiochatty run` connects the session it
# started, `scripts/session_start_hook.py` connects the ones the wrapper could not name up
# front (`--resume`), and `/audiochatty-connect` remains for repair — retry a connect that
# failed while the backend was down, rename a session, reconnect one that was disconnected.
#
# All three go through `connect_session` below. They differ in exactly two ways: how the
# bind happens (an in-process call for the wrapper itself, `POST /bind` over loopback for
# the two that run inside the session) and how the outcome is reported (printed for the
# slash command, recorded in the rendezvous file for the silent paths). The *order* —
# register → bind → verified → marker — has one home, because the argument for it is
# subtle and would rot in a second copy.


class ConnectResult:
    """What happened, in a form both a printing caller and a silent one can use.

    `error` is machine-readable and is what `/audiochatty-status` turns into a cause;
    `detail` is one human line for the same purpose. `ok` is the only thing most callers
    check.
    """

    __slots__ = ("ok", "error", "detail", "name", "agent_session_id")

    def __init__(
        self,
        ok: bool,
        *,
        error: str = "",
        detail: str = "",
        name: str = "",
        agent_session_id: str = "",
    ):
        self.ok = ok
        self.error = error
        self.detail = detail
        self.name = name
        self.agent_session_id = agent_session_id


def default_session_name(repo_path: str) -> str:
    """The working directory's basename, which is how a session shows up in the inbox.

    Shared so `audiochatty run --name` and `/audiochatty-connect [name]` cannot drift into
    naming the same session two different things.
    """
    return os.path.basename(repo_path.rstrip("/")) or "claude-code"


def connect_session(
    *,
    claude_session_id: str,
    repo_path: str,
    name: str,
    token: str,
    base_url: str,
    bind,
    wrapper_pid=None,
    wrapper_port=None,
    skip_next_turn: bool,
) -> ConnectResult:
    """Register this session, bind its return path, say it's reachable, open the gate.

    The order is load-bearing and has not changed since the channel design: `/bind` needs
    the `agent_sessions.id` that registration returns, `/agent/session/verified` states a
    fact that only becomes true at the bind (W8), and the marker is the Stop hook's gate,
    which must not open until the rest worked.

    `bind` is a one-argument callable taking the `agent_session_id` and raising `ApiError`
    or `TransportError` if it fails. That is the whole of the difference between the
    wrapper connecting itself (an in-process `WrapperState.bind`) and something inside the
    session connecting it (`POST /bind` on loopback) — and it is a callable rather than a
    flag so this function never has to know which world it is in.

    **`skip_next_turn` is `True` only for the slash command.** That flag exists because the
    `/audiochatty-connect` turn's entire content is the model relaying "this session is now
    X in audiochatty", which is not worth reading back to the person who just typed it. A
    launch has no such turn, so on the automatic paths the session's first turn is real work
    and must not be swallowed.
    """
    try:
        response = post(
            "/agent/session",
            {"claude_session_id": claude_session_id, "name": name, "repo_path": repo_path},
            token=token,
            base_url=base_url,
        )
    except ApiError as exc:
        if exc.status == 401:
            return ConnectResult(
                False,
                error="revoked",
                detail="This machine's audiochatty token was revoked.",
            )
        return ConnectResult(
            False, error="rejected", detail=f"audiochatty refused the registration: {exc}"
        )
    except TransportError as exc:
        return ConnectResult(
            False, error="unreachable", detail=f"Couldn't reach audiochatty ({exc})."
        )

    registered_name = str(response.get("name") or name)
    agent_session_id = str(response.get("session_id") or "")

    try:
        bind(agent_session_id)
    except (ApiError, TransportError) as exc:
        # Registration already landed, so this is the one path that has to undo something.
        # Best effort, and the failure of the undo is survivable: what it leaves is a
        # session with no marker, which sends nothing and reads as unreachable in the inbox
        # — a degraded state the frontend already has somewhere to show.
        _end_session_quietly(claude_session_id, token, base_url)
        return ConnectResult(
            False,
            error="bind_failed",
            detail=f"Couldn't connect this session's return path ({exc}).",
        )

    # W8. The bind *is* the proof of reachability, so this states it rather than proving it:
    # there is no nonce, no injected handshake, no tool call for the model to answer, and no
    # retry loop here. Failure is not fatal and deliberately not reported — the wrapper
    # retries this from its own poll loop, so the cost of losing the call is that the phone
    # shows this session as unreachable for a few seconds.
    _mark_verified_quietly(claude_session_id, token, base_url)

    marker = {
        "claude_session_id": claude_session_id,
        # The backend's uuid for the registration, kept for /audiochatty-status and for
        # debugging a message that arrived under the wrong name.
        "session_id": response.get("session_id"),
        "name": registered_name,
        "repo_path": repo_path,
        "registered_at": _now_iso(),
        # Which wrapper process this session was bound to. `/audiochatty-disconnect`
        # unbinds by rendezvous file rather than by this, so it is here to make a
        # confusing session debuggable, not to be trusted — a pid is reused eventually.
        "wrapper_pid": wrapper_pid,
        "wrapper_port": wrapper_port,
    }
    if skip_next_turn:
        marker["skip_next_turn"] = True
    write_private_json(marker_path(claude_session_id), marker)
    reset_breaker()
    return ConnectResult(
        True, name=registered_name, agent_session_id=agent_session_id
    )


def cmd_connect(args: argparse.Namespace) -> int:
    """`/audiochatty-connect` — the repair tool (W13).

    Deterministic by design (D1): read the session id, find the wrapper, POST, write a
    marker. Nothing here is a judgment call, which is why it is a slash command and not a
    skill — routing it through the model means it can be paraphrased, skipped, or done
    twice.

    **This is no longer the way sessions get connected.** `audiochatty run` does that
    itself, so what is left here are the three cases it cannot cover: a connect that failed
    because the backend was down at launch, a rename, and reconnecting a session that was
    deliberately disconnected. All three are worth not having to restart Claude Code for.

    **The wrapper is still checked before anything is registered** (W4, unchanged from R1).
    Both refusals below are local and cost nothing, and taking them first is what keeps a
    refused `connect` from leaving a live session row behind.
    """
    token = device_token()
    if not token:
        print("This machine isn't paired with audiochatty yet. Run /audiochatty-login first.")
        return 1

    claude_session_id = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not claude_session_id:
        print("Couldn't tell which Claude Code session this is, so there's nothing to register.")
        return 1

    repo_path = args.cwd or os.getcwd()
    asked_for_name = bool((args.name or "").strip())
    name = (args.name or "").strip() or default_session_name(repo_path)

    wrapper, problem = find_wrapper(claude_session_id)
    if problem == "session_mismatch":
        _print_nested_session()
        return 1
    if wrapper is None:
        _print_relaunch(
            "This session has no audiochatty return path, so it can't be talked to — and a\n"
            "session you can't talk to isn't worth registering."
        )
        return 1

    # The likely case now, and one this command never had to handle while connecting was a
    # required step. Re-registering a working session would be harmless but pointless — so
    # say so instead, *unless* a name was asked for, since renaming is one of the three
    # reasons to run this at all.
    marker = load_marker(claude_session_id)
    already_bound = str(wrapper.get("claude_session_id") or "") == claude_session_id
    if marker and already_bound and not asked_for_name:
        print(f'This session is already connected as "{marker.get("name")}".')
        print("You can hear what it does, and tell it what to do next, from audiochatty.")
        return 0

    result = connect_session(
        claude_session_id=claude_session_id,
        repo_path=repo_path,
        name=name,
        token=token,
        base_url=backend_url(args.backend_url),
        bind=lambda agent_session_id: bind_wrapper(
            wrapper,
            agent_session_id=agent_session_id,
            claude_session_id=claude_session_id,
            base=backend_url(args.backend_url),
            token=token,
            name=name,
        ),
        wrapper_pid=wrapper.get("pid"),
        wrapper_port=wrapper.get("port"),
        skip_next_turn=True,
    )

    if not result.ok:
        if result.error == "revoked":
            print("This machine's audiochatty token was revoked. ")
            print("Run /audiochatty-login to pair again.")
            return 1
        if result.error == "bind_failed":
            print(result.detail)
            print("Nothing was registered. Try /audiochatty-connect again; if it keeps failing,")
            print(f"exit and start the session again with `{RUN_COMMAND}`.")
            return 1
        print(result.detail)
        print("This session is not registered.")
        return 1

    # An explicit reconnect is the one thing that overrides a `disconnect` (W13): the user
    # is asking for it by name, where a hook firing after a `/clear` is not.
    clear_tombstone(claude_session_id)
    print(f'This session is now "{result.name}" in audiochatty.')
    print("You can hear what it does, and tell it what to do next, from audiochatty.")
    return 0


# -- status ----------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    """Local-only, on purpose: no endpoint, no token spent, no network to be slow. Every
    question it answers is answered by a file this machine owns."""
    credentials = load_credentials()
    claude_session_id = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID", "")

    if not credentials.get("token"):
        print("This machine isn't paired with audiochatty. Run /audiochatty-login to pair it.")
        return 0

    workspace = credentials.get("workspace_name") or "audiochatty"
    who = credentials.get("profile_name")
    print(f"Paired with {workspace}" + (f" as {who}." if who else "."))
    if credentials.get("label"):
        print(f"This machine is \"{credentials['label']}\".")

    marker = load_marker(claude_session_id)
    if marker:
        print(f'This session is registered as "{marker.get("name")}".')
        print("Every turn you finish here is sent to audiochatty.")
    elif session_was_disconnected(claude_session_id):
        # Distinguished from "never connected" because the fix is different and because a
        # user who ran /audiochatty-disconnect should be told it held rather than left
        # wondering whether it worked.
        print("This session was disconnected — nothing from it is being sent, and it stays")
        print("that way for the rest of the session. Run /audiochatty-connect to undo that.")
    else:
        print("This session is NOT registered — nothing from it is being sent.")
        print("Run /audiochatty-connect [name] to start.")

    _print_wrapper_status(claude_session_id, registered=bool(marker))

    others = _other_registered_sessions(claude_session_id)
    if others:
        listed = ", ".join(f'"{name}"' for name in others[:5])
        more = f" (+{len(others) - 5} more)" if len(others) > 5 else ""
        print(f"Other registered sessions on this machine: {listed}{more}.")
    return 0


def _print_wrapper_status(claude_session_id: str, *, registered: bool) -> None:
    """The return path, from files this machine owns.

    Still no network, including no loopback call to the wrapper: it writes `bound` and
    `verified` into its own rendezvous file as they change, so reading the file answers the
    same question a `GET /status` would. That keeps the promise this command makes — nothing
    here can be slow, and nothing here can fail because something else is down.

    This is the command a confused user runs, so it names the *cause* where it can tell.
    There is one fewer cause to name than there used to be: the old "connected but
    unconfirmed because no flag was passed" case cannot happen, since verification is now
    set at bind time rather than proven on a later turn (W8).

    **After W13 this command carries more weight than it used to.** Connecting happens at
    launch and prints nothing, so success and failure look identical in the terminal — this
    is the only place a failed connect surfaces at all. The wrapper writes why it failed
    into its rendezvous file precisely so this function can say it out loud.
    """
    wrapper, problem = find_wrapper(claude_session_id)

    if problem == "session_mismatch":
        print("This session is running inside an audiochatty wrapper that belongs to a")
        print("different session — a plain `claude` started from inside a wrapped one. It")
        print("can't be talked to, and connecting it would type into the outer terminal.")
        return
    if wrapper is None:
        print("This session has no audiochatty return path, so it can't be told what to do")
        print("next. Exit it and start Claude Code with:")
        print(f"    {RUN_COMMAND}")
        return

    # W13: the launch tried to connect and failed, silently, because there is nowhere on a
    # TUI's screen for it to have said so. This is where that reason comes out.
    connect_error = str(wrapper.get("connect_error") or "")
    if connect_error and not registered:
        print(_connect_error_line(connect_error))
        return

    # The `claude_session_id` guard matters: without it an *unbound* wrapper (whose
    # `claude_session_id` is null, so `""`) reads as bound to us whenever we could not work
    # out our own session id either. Two unknowns comparing equal is not a match.
    bound_to_us = (
        bool(claude_session_id)
        and str(wrapper.get("claude_session_id") or "") == claude_session_id
    )
    if not bound_to_us:
        if registered:
            # A marker with an unbound wrapper is the shape a `/clear` cannot produce and
            # `/audiochatty-disconnect` on the wrapper side can: the registration outlived
            # the binding.
            print("Its audiochatty return path isn't connected, so it can't receive")
            print("instructions. Run /audiochatty-connect again to reconnect it.")
        else:
            print("An audiochatty wrapper is running and waiting to be connected.")
        return

    if wrapper.get("verified"):
        print("You can talk to this session from audiochatty — the return path is confirmed.")
        return

    # Only reachable from a hand-edited rendezvous file: the wrapper sets `verified` in the
    # same write as `bound`. Worth a line rather than silence, because the alternative is a
    # status command that prints nothing at all about the half the user asked about.
    print("Its audiochatty return path is connected but not confirmed, so audiochatty will")
    print("say this session can't be talked to. Run /audiochatty-connect again.")


def _connect_error_line(error: str) -> str:
    """One line per `ConnectResult.error`, ending in what to do about it.

    Kept as a mapping here rather than storing the human sentence in the rendezvous file:
    the file records *what happened*, and the advice for it belongs with the command that
    gives advice. A code this doesn't know about still produces something honest.
    """
    if error == "revoked":
        return (
            "This session tried to connect at launch and audiochatty rejected the "
            "machine's token.\nRun /audiochatty-login to pair again, then "
            "/audiochatty-connect."
        )
    if error == "unreachable":
        return (
            "This session tried to connect at launch and couldn't reach audiochatty, so it\n"
            "can't be talked to. Run /audiochatty-connect to try again — the session doesn't\n"
            "need restarting."
        )
    if error == "bind_failed":
        return (
            "This session's return path didn't connect at launch, so it can't be talked to.\n"
            "Run /audiochatty-connect to try again."
        )
    return (
        "This session tried to connect at launch and didn't manage it "
        f"({error}).\nRun /audiochatty-connect to try again."
    )


def _other_registered_sessions(claude_session_id: str) -> list[str]:
    sessions = config_dir() / "sessions"
    if not sessions.is_dir():
        return []
    names = []
    for path in sorted(sessions.glob("*.json")):
        if path.stem == _safe_filename(claude_session_id):
            continue
        name = read_json(path).get("name")
        if name:
            names.append(str(name))
    return names


# -- disconnect ------------------------------------------------------------------------


def cmd_disconnect(args: argparse.Namespace) -> int:
    """Remove the marker, unbind the wrapper, then tell the backend.

    **The marker goes first, and that ordering is the whole point.** The marker is what
    the Stop hook consults, so deleting it stops the flow of turns immediately and
    locally. If the POST then fails, the worst outcome is a row that reads `active` in
    somebody's settings list while producing nothing — strictly better than the reverse,
    where a failed delete leaves a hook happily posting to a session the user believes
    they closed.

    The unbind is second for the same reason and by the same argument: it is local, it
    cannot fail in a way that matters, and it stops the *other* direction — a wrapper that
    stays bound keeps polling for instructions addressed to a session the user has just
    closed, and would type one into this terminal.

    **The tombstone is third, and it is what makes this stick** (W13). Connecting is
    automatic now, and `SessionStart` fires again on `/clear` in the same session — so
    without a record that the user closed this one, a `/clear` a minute later would quietly
    reconnect it. Written after the local teardown because it is the least urgent part: its
    absence costs stickiness across a later `/clear`, while a marker left in place would
    keep sending turns right now.
    """
    claude_session_id = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not claude_session_id:
        print("Couldn't tell which Claude Code session this is.")
        return 1

    marker = load_marker(claude_session_id)
    marker_path(claude_session_id).unlink(missing_ok=True)

    # By rendezvous file rather than by the marker's `wrapper_port`: the file is what says a
    # *live* process is on that port and which session it currently holds, and a port
    # remembered from an hour ago may belong to something else entirely by now.
    #
    # Every live wrapper is scanned rather than just the one the environment names, and the
    # filter is the binding rather than the pid. That is deliberate: this has to work in the
    # session that is being retired even if its wrapper's variables never made it here, and
    # a wrapper bound to *this* session is ours by definition however we found it.
    token = device_token()
    for wrapper in read_wrappers():
        if str(wrapper.get("claude_session_id") or "") == claude_session_id:
            unbind_wrapper(wrapper, token)

    write_tombstone(claude_session_id)

    if not marker:
        print("This session wasn't registered with audiochatty.")
        return 0

    name = marker.get("name") or "this session"
    if not token:
        print(f'Stopped sending "{name}" to audiochatty.')
        return 0

    _end_session_quietly(claude_session_id, token, args.backend_url)
    print(f'Stopped sending "{name}" to audiochatty.')
    return 0


def _end_session_quietly(claude_session_id: str, token: str, base: str | None) -> None:
    """`POST /agent/session/end`, swallowing everything.

    Two callers with the same need: `disconnect`, where local state is already correct and
    a failure is a background detail the user did not ask about, and `connect`'s rollback,
    where the alternative to a best-effort undo is no undo at all.
    """
    try:
        post(
            "/agent/session/end",
            {"claude_session_id": claude_session_id},
            token=token,
            base_url=backend_url(base),
        )
    except (ApiError, TransportError):
        pass


def _mark_verified_quietly(claude_session_id: str, token: str, base: str | None) -> bool:
    """`POST /agent/session/verified` — the whole of W8, in one call that may fail.

    The inbox reads `agent_sessions.channel_verified_at` to decide whether the user is
    offered a reply at all, so this is the call that makes a connected session *look*
    connected on the phone. It is still not worth failing `connect` over: the wrapper retries
    it from its own poll loop, so a lost call costs a few seconds of the phone saying "you
    can't talk to this one" and nothing else. Nothing is printed either way — a user who
    just watched `connect` succeed has no use for a warning about a call that will be made
    again without them.

    The column keeps its old name on purpose. There is no channel any more, but renaming it
    is a migration plus four call sites for no behaviour change, and it still means exactly
    what it always meant: this session can be reached.
    """
    try:
        post(
            "/agent/session/verified",
            {"claude_session_id": claude_session_id},
            token=token,
            base_url=backend_url(base),
        )
    except (ApiError, TransportError):
        return False
    return True


# -- the ingest calls the hooks make ---------------------------------------------------


def post_turn(claude_session_id: str, payload: dict) -> str:
    """`POST /agent/turn`, with every failure swallowed. Returns a short reason string for
    tests and logs; the hook itself never surfaces it.

    Ordered cheapest-first, because this runs on every completed turn in every registered
    session: the breaker (a file read) before the token (a file read) before the network.
    """
    if breaker_is_open():
        return "breaker-open"
    token = device_token()
    if not token:
        return "not-paired"

    body = dict(payload)
    body["claude_session_id"] = claude_session_id
    try:
        post("/agent/turn", body, token=token, timeout=HOOK_TIMEOUT)
    except TransportError:
        trip_breaker()
        return "unreachable"
    except ApiError as exc:
        # A reachable backend that said no is not an outage — a 404 is the ordinary
        # answer for a session the user disconnected, and tripping the breaker on it
        # would silence a session that is working fine.
        return f"rejected-{exc.status}"
    reset_breaker()
    return "sent"


def post_session_end(claude_session_id: str) -> str:
    """`POST /agent/session/end` from the SessionEnd hook. Same silence, same reasons."""
    if breaker_is_open():
        return "breaker-open"
    token = device_token()
    if not token:
        return "not-paired"
    try:
        post(
            "/agent/session/end",
            {"claude_session_id": claude_session_id},
            token=token,
            timeout=HOOK_TIMEOUT,
        )
    except TransportError:
        trip_breaker()
        return "unreachable"
    except ApiError as exc:
        return f"rejected-{exc.status}"
    return "sent"


# -- odds and ends ---------------------------------------------------------------------


def _default_label() -> str:
    """What the approving browser sees. The hostname, because the question it answers is
    "which machine am I about to link", and that is the only thing this side knows that
    the browser doesn't."""
    try:
        host = socket.gethostname().split(".")[0]
    except OSError:
        host = ""
    return (host or "a terminal")[:64]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audiochatty", description="audiochatty for Claude Code"
    )
    parser.add_argument(
        "--backend-url",
        default=None,
        help="Override the audiochatty backend (also AUDIOCHATTY_BACKEND_URL).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="Pair this machine with audiochatty.")
    login.add_argument("--label", default=None, help="What to call this machine.")
    login.add_argument("--wait", type=float, default=DEFAULT_LOGIN_WAIT)
    login.add_argument("--reset", action="store_true", help="Discard a pending code.")
    login.set_defaults(func=cmd_login)

    connect = sub.add_parser("connect", help="Register this session.")
    connect.add_argument("name", nargs="?", default=None)
    connect.add_argument("--session-id", default=None)
    connect.add_argument("--cwd", default=None)
    connect.set_defaults(func=cmd_connect)

    status = sub.add_parser("status", help="Show local pairing and registration state.")
    status.add_argument("--session-id", default=None)
    status.set_defaults(func=cmd_status)

    disconnect = sub.add_parser("disconnect", help="Retire this session.")
    disconnect.add_argument("--session-id", default=None)
    disconnect.set_defaults(func=cmd_disconnect)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("Cancelled.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
