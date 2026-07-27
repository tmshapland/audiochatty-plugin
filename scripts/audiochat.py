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
(0600) with the device token, a `sessions/` directory of marker files, and — only while a
pairing is in flight — a `pending.json` holding the `device_code`. Nothing else, and no
process: between turns the machine is doing nothing on audiochatty's behalf (§2).

**The token is never printed and never taken as an argument.** Anything typed into a
Claude Code prompt lands in the session `.jsonl` and in the model's context, and anything
this script prints does too, because a slash command's output *is* the prompt. So the
long-lived token travels backend → this process → 0600 file and stops there. The only
secret that ever reaches a terminal is the `user_code`, which is short-lived, single-use,
and useless without a signed-in browser.
"""

from __future__ import annotations

import argparse
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
HOOK_TIMEOUT = 2.5

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
    """
    url = f"{base_url or backend_url()}{path}"
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
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
    print("Run /audiochatty-connect in any session you want to hear about.")
    return 0


# -- connect ---------------------------------------------------------------------------


def cmd_connect(args: argparse.Namespace) -> int:
    """Register this session. Deterministic by design (D1): read the session id, write a
    marker, POST once. Nothing here is a judgment call, which is why it is a slash
    command and not a skill — routing it through the model means it can be paraphrased,
    skipped, or done twice."""
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
        },
    )
    reset_breaker()
    print(f'This session is now "{registered_name}" in audiochatty.')
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

    others = _other_registered_sessions(claude_session_id)
    if others:
        listed = ", ".join(f'"{name}"' for name in others[:5])
        more = f" (+{len(others) - 5} more)" if len(others) > 5 else ""
        print(f"Other registered sessions on this machine: {listed}{more}.")
    return 0


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
    """Remove the marker, then tell the backend.

    **The marker goes first, and that ordering is the whole point.** The marker is what
    the Stop hook consults, so deleting it stops the flow of turns immediately and
    locally. If the POST then fails, the worst outcome is a row that reads `active` in
    somebody's settings list while producing nothing — strictly better than the reverse,
    where a failed delete leaves a hook happily posting to a session the user believes
    they closed.
    """
    claude_session_id = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not claude_session_id:
        print("Couldn't tell which Claude Code session this is.")
        return 1

    marker = load_marker(claude_session_id)
    marker_path(claude_session_id).unlink(missing_ok=True)

    if not marker:
        print("This session wasn't registered with audiochatty.")
        return 0

    name = marker.get("name") or "this session"
    token = device_token()
    if not token:
        print(f'Stopped sending "{name}" to audiochatty.')
        return 0

    try:
        post(
            "/agent/session/end",
            {"claude_session_id": claude_session_id},
            token=token,
            base_url=backend_url(args.backend_url),
        )
    except (ApiError, TransportError):
        # Local state is already correct. Saying more would be noise about a background
        # detail the user did not ask about.
        pass
    print(f'Stopped sending "{name}" to audiochatty.')
    return 0


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
