#!/usr/bin/env python3
"""The audiochatty plugin's whole client — shared library and CLI in one file.

`coding_agent_build_plan.md` Phase 5 · `coding_agent_summary_plan.md` §5.

**Stdlib only.** `urllib` and `json`, no pip, no venv, no build step. That constraint is
what makes "read the repo, then install it" an honest offer (§5), and it is easy to break
with one convenient import. There is nothing to add here that is worth a dependency.

Four subcommands, one per slash command, plus the two hook scripts that import this module:

    login       the RFC 8628 device flow — mint a code, then redeem it
    connect     register this Claude Code session under a name
    status      local-only: is this machine paired, is this session registered
    disconnect  retire this session

**What is on disk, and why so little.** `~/.audiochatty/` (0700) holds a credentials file
(0600) with the device token, a `sessions/` directory of marker files, a `channels/`
directory the channel server writes its rendezvous into, and — only while a pairing is in
flight — a `pending.json` holding the `device_code`. Nothing else.

**There is one long-lived process now, and it is not this one.** `channel/server.ts` is the
return path (`channel_return_path_plan.md` R2): Claude Code spawns it, it does nothing until
`connect` binds it, and everything this file does about it is in "the channel" section
below. This script itself is still what it was — short-lived, one job, then gone.

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
import subprocess
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


# -- the channel (R4) ------------------------------------------------------------------
#
# The return path is a second process — `channel/server.ts`, an MCP server Claude Code
# spawns when the plugin is enabled — and this section is how a slash command running
# *inside* a session finds the one that belongs to it.
#
# The problem is that the channel starts before `/audiochatty-connect` does, and one
# machine can have fifteen of them. Nothing may inject into the wrong terminal. So each
# channel writes `~/.audiochatty/channels/<pid>.json` describing itself, and this side
# picks the one that shares its `claude` process. Three signals, any one of which is
# enough, because they fail independently:
#
#   1. `claude_env.CLAUDE_SESSION_ID` — exact, if that variable reaches an MCP server.
#   2. `claude_env.CLAUDE_PID` — exact, if that one does.
#   3. the channel's recorded ancestry containing our own `claude` pid.
#
# `CLAUDE_PID` is set in *this* process's environment and equals the `claude` in our own
# ancestry (measured, 2026-07-28), which is what makes (2) and (3) usable from here.
#
# **Ambiguity is an error, never a guess.** Zero matches and two matches each get their own
# refusal, and neither registers anything.

CHANNEL_TIMEOUT = 3.0

# What a session has to be started with for its channel's events to be honoured. Not
# optional and not evadable: during the research preview a channel must be on an
# Anthropic-curated allowlist to register, ours is not on it, and the community-marketplace
# submission does not add it. Both ways in — the allowlist (`--channels`) and the
# development flag — name the plugin on the command line, which is what makes the check in
# `channels_flag_names_us` a fair test rather than a guess about the future.
LAUNCH_COMMAND = "claude --dangerously-load-development-channels plugin:audiochatty@audiochatty"

CHANNEL_FLAGS = ("--dangerously-load-development-channels", "--channels")


def channels_dir() -> Path:
    """`~/.audiochatty/channels`, the same directory `channel/server.ts` writes to."""
    directory = config_dir() / "channels"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _process_table() -> dict[int, tuple[int, str]]:
    """`{pid: (ppid, comm)}` from one `ps`.

    One call for the whole table rather than one per generation: this runs in front of a
    slash command a person is waiting on. `-Ao` is accepted by both BSD `ps` (macOS) and
    procps (Linux), and the channel builds its half of the ancestry the same way.
    """
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}

    table: dict[int, tuple[int, str]] = {}
    for line in out.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
        except ValueError:
            continue
    return table


def claude_pid() -> int | None:
    """The `claude` process this command is running under.

    `CLAUDE_PID` is exported into the environment of everything Claude Code spawns, so the
    ancestry walk is the fallback rather than the mechanism. The walk stops at the first
    process whose command is `claude` — on macOS `comm` is a full path, so match the
    basename.
    """
    from_env = os.environ.get("CLAUDE_PID", "").strip()
    if from_env.isdigit():
        return int(from_env)

    table = _process_table()
    pid = os.getpid()
    seen = set()
    for _ in range(32):
        if pid <= 1 or pid in seen or pid not in table:
            return None
        seen.add(pid)
        ppid, comm = table[pid]
        if os.path.basename(comm.strip()) == "claude":
            return pid
        pid = ppid
    return None


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


def read_channels() -> list[dict]:
    """Every live channel on this machine, from its rendezvous file.

    A file whose process is gone is skipped rather than deleted — the channel prunes those
    itself at startup, and a slash command that garbage-collects another process's state is
    a slash command that races with it.
    """
    found = []
    try:
        entries = sorted(channels_dir().glob("*.json"))
    except OSError:
        return []
    for path in entries:
        if not path.stem.isdigit():
            continue
        data = read_json(path)
        pid = data.get("pid")
        port = data.get("port")
        if not isinstance(pid, int) or not isinstance(port, int) or port <= 0:
            continue
        if not _pid_is_alive(pid):
            continue
        found.append(data)
    return found


def _channel_matches(channel: dict, claude_session_id: str, my_claude_pid: int | None) -> bool:
    env = channel.get("claude_env")
    env = env if isinstance(env, dict) else {}

    if claude_session_id and str(env.get("CLAUDE_SESSION_ID") or "") == claude_session_id:
        return True
    if my_claude_pid is not None:
        if str(env.get("CLAUDE_PID") or "").strip() == str(my_claude_pid):
            return True
        ancestry = channel.get("ancestry")
        if isinstance(ancestry, list):
            for entry in ancestry:
                if isinstance(entry, dict) and entry.get("pid") == my_claude_pid:
                    return True
    return False


def find_channel(claude_session_id: str) -> tuple[dict | None, str]:
    """The channel belonging to this session, or why there isn't one.

    Returns `(channel, "")`, `(None, "none")`, or `(None, "ambiguous")`. A channel already
    bound to *another* session is not a candidate — it belongs to somebody else's terminal
    — while one bound to ours is exactly what `/audiochatty-connect` run twice should find.
    """
    my_claude_pid = claude_pid()
    live = read_channels()
    matches = [c for c in live if _channel_matches(c, claude_session_id, my_claude_pid)]
    _debug_channels(claude_session_id, my_claude_pid, len(live), len(matches))

    ours = [c for c in matches if str(c.get("claude_session_id") or "") == claude_session_id]
    if ours:
        # Already bound to us: a re-run in the same terminal. `/bind` treats it as a
        # refresh and re-probes if the last handshake went unanswered.
        return ours[0], ""

    free = [c for c in matches if not c.get("bound")]
    if len(free) == 1:
        return free[0], ""
    if not free:
        return None, "none"
    return None, "ambiguous"


def _debug_channels(
    claude_session_id: str, my_claude_pid: int | None, live: int, matched: int
) -> None:
    """`AUDIOCHATTY_DEBUG=1` explains a refusal.

    Correlation is the part of this design most likely to be wrong on a machine nobody has
    tried it on, and "no channel found" is indistinguishable from "found it, rejected it"
    without this.
    """
    if not os.environ.get("AUDIOCHATTY_DEBUG"):
        return
    print(
        f"[audiochatty] claude pid={my_claude_pid} session={claude_session_id} "
        f"channels={live} matched={matched}",
        file=sys.stderr,
    )


def channels_flag_names_us(pid: int | None) -> bool | None:
    """Was this session started with a channels flag naming audiochatty?

    **This is the check that makes R1 enforceable, and it exists because the obvious one
    does not work.** A channel server starts whenever the plugin is enabled; the flag
    decides only whether its notifications are *honoured*, and an unhonoured notification is
    dropped with no error. So a bind succeeds either way, and the handshake that would prove
    the difference cannot be answered until this command has returned and the model's turn
    begins. The command line is the one place the answer is legible at the moment we need
    it.

    Returns True, False, or **None for "could not tell"** — a `ps` that fails must not
    become a refusal, since the cost of failing closed here is a plugin that never registers
    anything.
    """
    if pid is None:
        return None

    try:
        out = subprocess.run(
            ["ps", "-ww", "-o", "args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None

    tokens = out.stdout.strip().split()
    values: list[str] = []
    for index, token in enumerate(tokens):
        for flag in CHANNEL_FLAGS:
            if token == flag and index + 1 < len(tokens):
                values.append(tokens[index + 1])
            elif token.startswith(f"{flag}="):
                values.append(token[len(flag) + 1 :])
    # Deliberately loose about the entry's shape: `plugin:audiochatty@audiochatty` today,
    # `server:audiochatty` for a bare `.mcp.json`, and whatever an allowlisted future spells
    # it as. Comma-separated lists are split because a session may load several channels.
    # What is not loose is that some channels flag has to name us at all.
    return any("audiochatty" in entry for value in values for entry in value.split(","))


def bind_channel(channel: dict, *, agent_session_id: str, claude_session_id: str,
                 base: str, token: str, name: str) -> dict:
    """`POST /bind` on the channel's loopback port. Raises `ApiError`/`TransportError`."""
    return post(
        "/bind",
        {
            "agent_session_id": agent_session_id,
            "claude_session_id": claude_session_id,
            "backend_url": base,
            "token": token,
            "session_name": name,
        },
        base_url=f"http://127.0.0.1:{int(channel['port'])}",
        timeout=CHANNEL_TIMEOUT,
    )


def unbind_channel(channel: dict, token: str | None) -> bool:
    """`POST /unbind`, best effort. A channel that cannot be reached is one that has already
    exited, which is the same outcome by a different route."""
    try:
        post(
            "/unbind",
            {"token": token or ""},
            base_url=f"http://127.0.0.1:{int(channel['port'])}",
            timeout=CHANNEL_TIMEOUT,
        )
    except (ApiError, TransportError, KeyError, TypeError, ValueError):
        return False
    return True


def _print_relaunch(reason: str) -> None:
    print(reason)
    print()
    print("Start it again with the channel flag:")
    print()
    print(f"    {LAUNCH_COMMAND}")
    print()
    print("Claude Code will show a warning about development channels — that is expected;")
    print("channels are in research preview. Then run /audiochatty-connect again.")


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
    # R12. Four surfaces describe this setup — two manifest descriptions, the README, and
    # here — and this is the only one whose words we fully control and the only one the
    # user is looking at when the step is actually due. So it carries the command and the
    # reason, not a pointer to somewhere else that carries them.
    print("One more step, and it's per session rather than per machine: audiochatty needs")
    print("Claude Code's channel flag before a session can be registered at all. Start")
    print("Claude Code with:")
    print()
    print(f"    {LAUNCH_COMMAND}")
    print()
    print("then run /audiochatty-connect there. Without the flag, /audiochatty-connect")
    print("refuses outright — the session sends nothing and receives nothing.")
    return 0


# -- connect ---------------------------------------------------------------------------


def cmd_connect(args: argparse.Namespace) -> int:
    """Register this session, and bind the channel that lets it be talked to.

    Deterministic by design (D1): read the session id, find the channel, POST twice, write
    a marker. Nothing here is a judgment call, which is why it is a slash command and not a
    skill — routing it through the model means it can be paraphrased, skipped, or done
    twice.

    **The channel is checked before anything is registered** (R1). Both refusals below are
    local and cost nothing, and taking them first is what keeps a refused `connect` from
    leaving a live session row behind. The order after that is registration → bind →
    marker, because `/bind` needs the `agent_sessions.id` that registration returns and the
    marker is the Stop hook's gate, which must not open until the whole thing worked.
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
    name = (args.name or "").strip() or os.path.basename(repo_path.rstrip("/")) or "claude-code"

    channel, problem = find_channel(claude_session_id)
    if problem == "ambiguous":
        print("Found more than one audiochatty channel for this terminal, and there is no")
        print("safe way to tell which one is this session.")
        print()
        print("Binding the wrong one would deliver what you say into a different terminal,")
        print("so nothing was registered. Close the other Claude Code sessions started from")
        print("this window, or run /audiochatty-connect from a fresh terminal.")
        return 1
    if channel is None:
        _print_relaunch(
            "This session has no audiochatty channel, so it can't be talked to — and a\n"
            "session you can't talk to isn't worth registering."
        )
        return 1

    # A channel process running is not the same as a session whose channel events are
    # honoured. See `channels_flag_names_us`: this is the only moment the difference is
    # visible, and a session that fails it would register, bind, and then silently swallow
    # every instruction spoken to it.
    if channels_flag_names_us(claude_pid()) is False:
        _print_relaunch(
            "This session was started without Claude Code's channel flag, so audiochatty\n"
            "will not connect to it at all — not even to listen. Nothing was registered."
        )
        return 1

    try:
        response = post(
            "/agent/session",
            {"claude_session_id": claude_session_id, "name": name, "repo_path": repo_path},
            token=token,
            base_url=backend_url(args.backend_url),
        )
    except ApiError as exc:
        if exc.status == 401:
            print(
                "This machine's audiochatty token was revoked. "
                "Run /audiochatty-login to pair again."
            )
            return 1
        print(f"Couldn't register this session: {exc}")
        return 1
    except TransportError as exc:
        print(f"Couldn't reach audiochatty ({exc}). This session is not registered.")
        return 1

    registered_name = response.get("name") or name
    agent_session_id = str(response.get("session_id") or "")

    try:
        bind_channel(
            channel,
            agent_session_id=agent_session_id,
            claude_session_id=claude_session_id,
            base=backend_url(args.backend_url),
            token=token,
            name=registered_name,
        )
    except (ApiError, TransportError) as exc:
        # Registration already landed, so this is the one path that has to undo something.
        # Best effort, and the failure of the undo is survivable: what it leaves is a
        # session with no marker, which sends nothing and reads as unreachable in the inbox
        # — the same degraded state as a session whose handshake went unanswered, which
        # Phase 6 already has somewhere to show.
        _end_session_quietly(claude_session_id, token, args.backend_url)
        print(f"Couldn't connect this session's audiochatty channel ({exc}).")
        print("Nothing was registered. Try /audiochatty-connect again; if it keeps failing,")
        print("start the session again with:")
        print()
        print(f"    {LAUNCH_COMMAND}")
        return 1

    write_private_json(
        marker_path(claude_session_id),
        {
            "claude_session_id": claude_session_id,
            # The backend's uuid for the registration, kept for /audiochatty-status and for
            # debugging a message that arrived under the wrong name.
            "session_id": response.get("session_id"),
            "name": registered_name,
            "repo_path": repo_path,
            "registered_at": _now_iso(),
            # Which channel process this session was bound to. `/audiochatty-disconnect`
            # unbinds by rendezvous file rather than by this, so it is here to make a
            # confusing session debuggable, not to be trusted — a pid is reused eventually.
            "channel_pid": channel.get("pid"),
            "channel_port": channel.get("port"),
            # This very turn ends with the model relaying the line printed below, and the
            # marker it would be checked against already exists. `consume_skip_next_turn`
            # spends this on that turn so registration itself is never a message.
            #
            # It is now also the turn the channel's handshake lands in — the `/bind` above
            # injects one — so the flag covers both halves of setup with a single turn,
            # which is the right number: the user watched all of it happen.
            "skip_next_turn": True,
        },
    )
    reset_breaker()
    print(f'This session is now "{registered_name}" in audiochatty.')
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
    else:
        print("This session is NOT registered — nothing from it is being sent.")
        print("Run /audiochatty-connect [name] to start.")

    _print_channel_status(claude_session_id, registered=bool(marker))

    others = _other_registered_sessions(claude_session_id)
    if others:
        listed = ", ".join(f'"{name}"' for name in others[:5])
        more = f" (+{len(others) - 5} more)" if len(others) > 5 else ""
        print(f"Other registered sessions on this machine: {listed}{more}.")
    return 0


def _print_channel_status(claude_session_id: str, *, registered: bool) -> None:
    """The return path, from files this machine owns.

    Still no network, including no loopback call to the channel: `server.ts` writes `bound`
    and `verified` into its own rendezvous file as they change, so reading the file answers
    the same question a `GET /status` would. That keeps the promise this command makes —
    nothing here can be slow, and nothing here can fail because something else is down.

    This is the command a confused user runs, so it names the *cause* where it can tell.
    An unverified channel has several, and only two of them are visible from here; the flag
    is one of those two, and it is by far the most common.
    """
    channel, problem = find_channel(claude_session_id)

    if problem == "ambiguous":
        print("More than one audiochatty channel matches this terminal, so audiochatty")
        print("won't bind any of them. Close the other sessions started from this window.")
        return
    if channel is None:
        print("No audiochatty channel is running for this session, so it can't be told what")
        print("to do next. Start the session again with:")
        print(f"    {LAUNCH_COMMAND}")
        return

    bound_to_us = str(channel.get("claude_session_id") or "") == claude_session_id
    if not bound_to_us:
        if registered:
            # A marker with an unbound channel is the shape a `/clear` cannot produce and a
            # channel restart can: the process this session bound to is gone and a new one
            # took its place, holding no binding.
            print("Its audiochatty channel isn't connected, so it can't receive instructions.")
            print("Run /audiochatty-connect again to reconnect it.")
        else:
            print("An audiochatty channel is running and waiting to be connected.")
        return

    if channel.get("verified"):
        print("You can talk to this session from audiochatty — the return path is confirmed.")
        return

    print("Its audiochatty channel is connected but unconfirmed, so audiochatty will say")
    print("this session can't be talked to.")
    if channels_flag_names_us(claude_pid()) is False:
        print("The reason is that this session was started without the channel flag:")
        print(f"    {LAUNCH_COMMAND}")
    else:
        print("It confirms itself on the first turn after connecting, so finish a turn and")
        print("check again. If it stays this way, run /audiochatty-connect again.")


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
    """Remove the marker, unbind the channel, then tell the backend.

    **The marker goes first, and that ordering is the whole point.** The marker is what
    the Stop hook consults, so deleting it stops the flow of turns immediately and
    locally. If the POST then fails, the worst outcome is a row that reads `active` in
    somebody's settings list while producing nothing — strictly better than the reverse,
    where a failed delete leaves a hook happily posting to a session the user believes
    they closed.

    The unbind is second for the same reason and by the same argument: it is local, it
    cannot fail in a way that matters, and it stops the *other* direction — a channel that
    stays bound keeps polling for instructions addressed to a session the user has just
    closed, and would deliver one into this terminal.
    """
    claude_session_id = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not claude_session_id:
        print("Couldn't tell which Claude Code session this is.")
        return 1

    marker = load_marker(claude_session_id)
    marker_path(claude_session_id).unlink(missing_ok=True)

    # By rendezvous file rather than by the marker's `channel_port`: the file is what says
    # a *live* process is on that port and which session it currently holds, and a port
    # remembered from an hour ago may belong to something else entirely by now.
    token = device_token()
    for channel in read_channels():
        if str(channel.get("claude_session_id") or "") == claude_session_id:
            unbind_channel(channel, token)

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
