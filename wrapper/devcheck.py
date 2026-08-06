#!/usr/bin/env python3
"""Drive a running wrapper by hand, for the checks a person has to make.

Some things only a human can confirm: that a wrapped session feels like a normal one, that
Claude Code's own prompt treats an injected paste as one message, that an instruction sent
mid-turn is queued. Those need a real session with a real person in it, and then they need
these three requests.

    python3 wrapper/devcheck.py show
    python3 wrapper/devcheck.py bind
    python3 wrapper/devcheck.py inject "some instruction"
    python3 wrapper/devcheck.py unbind

**This is a hand tool, not part of the product.** `/audiochatty-connect` is what really binds a
session; this stands in for it. It talks to the wrapper the same way, over the frozen local
API in `__main__.py`.

It refuses to guess when more than one wrapper is running — pass `--port` to pick one.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 5.0


def home() -> str:
    return os.environ.get("AUDIOCHATTY_HOME") or os.path.expanduser("~/.audiochatty")


def wrappers() -> list[dict]:
    found = []
    for path in sorted(glob.glob(os.path.join(home(), "wrappers", "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            continue
        record["_path"] = path
        found.append(record)
    return found


def token() -> str:
    try:
        with open(os.path.join(home(), "credentials.json"), encoding="utf-8") as handle:
            return str(json.load(handle).get("token") or "")
    except (OSError, ValueError):
        return ""


def pick(port: int | None) -> dict:
    running = wrappers()
    if port:
        for record in running:
            if int(record.get("port") or 0) == port:
                return record
        sys.exit(f"no wrapper listening on {port}")
    if not running:
        sys.exit("no wrapper running. Start one with `wrapper/audiochatty run`")
    if len(running) > 1:
        ports = ", ".join(str(r.get("port")) for r in running)
        sys.exit(f"{len(running)} wrappers running (ports {ports}) — pass --port")
    return running[0]


def request(record: dict, route: str, body: dict | None, method: str = "POST") -> None:
    url = f"http://127.0.0.1:{record['port']}/{route}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            print(response.status, json.dumps(json.load(response), indent=2))
    except urllib.error.HTTPError as err:
        print(err.code, err.read().decode())
    except OSError as err:
        sys.exit(f"could not reach {url}: {err}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, help="Which wrapper, if several are running.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="Every running wrapper, from its rendezvous file.")
    sub.add_parser("status", help="Ask one wrapper directly.")
    parser.add_argument(
        "--backend-url",
        help="Where the poll loop should ask for instructions. Defaults to an unreachable "
        "address, so a bind from here does not start talking to production by accident.",
    )
    sub.add_parser("bind", help="Stand in for /audiochatty-connect.")
    sub.add_parser("unbind", help="Stand in for /audiochatty-disconnect.")
    inject = sub.add_parser("inject", help="Type text into the session.")
    inject.add_argument("text", help="Use real newlines to test the multi-line case.")
    args = parser.parse_args(argv)

    if args.command == "show":
        running = wrappers()
        print(f"{len(running)} wrapper(s) running")
        interesting = (
            "pid", "child_pid", "port", "bound", "verified",
            "claude_session_id", "expected_session_id", "session_name",
        )
        for record in running:
            print(record["_path"])
            print(json.dumps({k: record.get(k) for k in interesting}, indent=2))
        return 0

    record = pick(args.port)

    if args.command == "status":
        # `polling` is the field to look at here: bound and not polling means the loop died,
        # which is a different problem from not being bound at all.
        request(record, "status", None, method="GET")
        return 0

    device_token = token()
    if not device_token:
        sys.exit(
            f"no device token in {home()}/credentials.json — "
            "run /audiochatty-pair-start then /audiochatty-pair-finish first"
        )

    if args.command == "bind":
        request(record, "bind", {
            "token": device_token,
            "agent_session_id": "devcheck-manual",
            # The wrapper refuses a session it did not start, so this has to be its own.
            "claude_session_id": record.get("expected_session_id") or "devcheck-session",
            # Unreachable unless you ask for something else. The poller starts at the
            # bind, so a real URL here means a hand test talking to production — pass
            # `--backend-url` deliberately when that is what you want.
            "backend_url": args.backend_url or "http://127.0.0.1:1",
            "session_name": "devcheck",
        })
    elif args.command == "unbind":
        request(record, "unbind", {"token": device_token})
    elif args.command == "inject":
        request(record, "inject", {"token": device_token, "text": args.text})
    return 0


if __name__ == "__main__":
    sys.exit(main())
