"""The channel server, driven the way Claude Code drives it — no Claude Code required.

`channel_return_path_plan.md` Phase 3.

Claude Code spawns `channel/server.ts` as a subprocess and speaks MCP to it over stdio, so
that is exactly what these tests do: a ~90-line JSON-RPC client below (`ChannelProcess`)
plays the client half, and `StubBackend` plays audiochatty. Between them, everything this
server does is observable without a browser, a phone, or a paid API call.

What is worth breaking here is what the plan singles out:

- **the rendezvous file** — its contents, its 0600 mode, and its removal on exit, because a
  stale one is how `/audiochatty-connect` binds to a dead process;
- **bind refusal** — a wrong token or a second bind, because the alternative is a channel
  that can be pointed at a session it does not own;
- **the payload on the wire** — an instruction arrives as one `notifications/claude/channel`
  with identifier-safe meta keys, nothing is sent before a bind, and nothing is *fetched*
  before the handshake is answered;
- **exactly-once in practice** (R6) — an id already injected is never injected again, even
  after a restart, and the ack is retried until the backend hears it;
- **the handshake** (R11) — the right nonce verifies and tells the backend, the wrong one
  does nothing;
- **silence under failure** — a backend that is down leaves the session untouched.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stub_backend import StubBackend  # noqa: E402

CHANNEL = Path(__file__).resolve().parent.parent / "channel"
SERVER = CHANNEL / "server.ts"
BUN = shutil.which("bun")

CLAUDE_SESSION_ID = "11111111-aaaa-4bbb-8ccc-222222222222"
AGENT_SESSION_ID = "33333333-3333-4333-8333-333333333333"
DEVICE_TOKEN = "stub-device-token"

# Long enough to cover a 5s poll plus process startup on a busy machine; short enough that
# a genuine failure fails the suite rather than hanging it.
WAIT = 20.0


def message(id_: str, text: str, sender: str = "Mike") -> dict:
    return {
        "id": id_,
        "text": text,
        "sender_name": sender,
        "created_at": "2026-07-27T18:04:11Z",
    }


class ChannelProcess:
    """One `bun server.ts`, plus the client half of MCP over stdio.

    Newline-delimited JSON-RPC in both directions. A reader thread splits what comes back
    into responses (they carry the `id` we sent) and server-initiated traffic, which for
    this server is only ever `notifications/claude/channel`.
    """

    def __init__(self, home: Path, env_extra: dict | None = None):
        env = dict(os.environ)
        env["AUDIOCHATTY_HOME"] = str(home)
        env.update(env_extra or {})
        self.proc = subprocess.Popen(
            [BUN, str(SERVER)],
            cwd=str(CHANNEL),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )
        self._responses: queue.Queue = queue.Queue()
        self.notifications: list[dict] = []
        self._lock = threading.Lock()
        self._next_id = 0
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.stderr: list[str] = []
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_reader.start()

    # -- plumbing --

    def _read_loop(self) -> None:
        for line in self.proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                # Anything unparseable on stdout is a bug in the server: that stream is
                # the transport. Keep it for the assertion in `test_stdout_is_only_mcp`.
                with self._lock:
                    self.notifications.append({"__garbage__": line})
                continue
            if "id" in parsed and ("result" in parsed or "error" in parsed):
                self._responses.put(parsed)
            else:
                with self._lock:
                    self.notifications.append(parsed)

    def _read_stderr(self) -> None:
        for line in self.proc.stderr:  # type: ignore[union-attr]
            self.stderr.append(line.rstrip())

    def send(self, payload: dict) -> None:
        self.proc.stdin.write(json.dumps(payload) + "\n")  # type: ignore[union-attr]
        self.proc.stdin.flush()  # type: ignore[union-attr]

    def request(self, method: str, params: dict | None = None, timeout: float = WAIT) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                response = self._responses.get(timeout=deadline - time.time())
            except queue.Empty:
                break
            if response.get("id") == request_id:
                return response
        raise AssertionError(f"no response to {method} in {timeout}s")

    def initialize(self) -> dict:
        """The MCP handshake, once per process — a second `initialize` is a protocol error,
        so the result is kept for the tests that assert on what was advertised."""
        response = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "audiochatty-tests", "version": "0"},
            },
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.init_result = response["result"]
        return self.init_result

    # -- assertions --

    def channel_events(self, kind: str | None = None) -> list[dict]:
        """Channel notifications, optionally only the handshakes (`kind='handshake'`) or
        only the real instructions (`kind='instruction'`)."""
        with self._lock:
            events = [n for n in self.notifications if n.get("method") == "notifications/claude/channel"]
        if kind == "handshake":
            return [e for e in events if e["params"].get("meta", {}).get("kind") == "handshake"]
        if kind == "instruction":
            return [e for e in events if "message_id" in e["params"].get("meta", {})]
        return events

    def wait_for_event(self, kind: str | None = None, count: int = 1, timeout: float = WAIT) -> list[dict]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            events = self.channel_events(kind)
            if len(events) >= count:
                return events
            time.sleep(0.05)
        raise AssertionError(f"no {kind or 'channel'} event (x{count}) in {timeout}s")

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def post_local(url: str, body: dict, timeout: float = 10.0) -> tuple[int, dict]:
    """One POST to the channel's own loopback port. Returns `(status, payload)` for the
    error cases too, since half of what is asserted here is a refusal."""
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def get_local(url: str, timeout: float = 10.0) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@unittest.skipUnless(BUN, "bun is not installed; the channel cannot be run")
class ChannelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.backend = StubBackend()
        self.backend.__enter__()
        self.addCleanup(lambda: self.backend.__exit__(None, None, None))
        (self.home / "credentials.json").write_text(
            json.dumps({"token": DEVICE_TOKEN, "backend_url": self.backend.url})
        )
        self.channels: list[ChannelProcess] = []

    def start(self, env_extra: dict | None = None) -> ChannelProcess:
        channel = ChannelProcess(self.home, env_extra)
        self.channels.append(channel)
        self.addCleanup(channel.close)
        channel.initialize()
        return channel

    def rendezvous_path(self, channel: ChannelProcess, timeout: float = WAIT) -> Path:
        path = self.home / "channels" / f"{channel.proc.pid}.json"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path.exists():
                return path
            time.sleep(0.05)
        raise AssertionError(f"no rendezvous file at {path} in {timeout}s")

    def rendezvous(self, channel: ChannelProcess) -> dict:
        return json.loads(self.rendezvous_path(channel).read_text())

    def bind_url(self, channel: ChannelProcess, route: str = "/bind") -> str:
        return f"http://127.0.0.1:{self.rendezvous(channel)['port']}{route}"

    def bind(
        self,
        channel: ChannelProcess,
        *,
        token: str = DEVICE_TOKEN,
        claude_session_id: str = CLAUDE_SESSION_ID,
        backend_url: str | None = None,
    ) -> tuple[int, dict]:
        return post_local(
            self.bind_url(channel),
            {
                "agent_session_id": AGENT_SESSION_ID,
                "claude_session_id": claude_session_id,
                "backend_url": backend_url if backend_url is not None else self.backend.url,
                "token": token,
                "session_name": "billing-refactor",
            },
        )

    def verify(self, channel: ChannelProcess) -> str:
        """Answer the handshake the way a session does. Polling does not start until this
        happens (see `pollLoop`), so most of what follows goes through here."""
        probe = channel.wait_for_event("handshake")[0]
        result = channel.request(
            "tools/call",
            {"name": "audiochatty_ack", "arguments": {"nonce": probe["params"]["meta"]["nonce"]}},
        )
        text = result["result"]["content"][0]["text"]
        self.assertTrue(text.startswith("verified"), text)
        return text

    def bind_and_verify(self, channel: ChannelProcess, **kwargs) -> tuple[int, dict]:
        response = self.bind(channel, **kwargs)
        self.verify(channel)
        return response

    def wait_for_request(self, path: str, count: int = 1, timeout: float = WAIT) -> list[dict]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            hits = self.backend.requests_to(path)
            if len(hits) >= count:
                return hits
            time.sleep(0.05)
        raise AssertionError(f"backend saw no {path} (x{count}) in {timeout}s")


# -- the rendezvous (R4) ----------------------------------------------------------------


class TestRendezvous(ChannelTestCase):
    def test_it_announces_itself_at_0600_with_a_live_port(self):
        channel = self.start()
        path = self.rendezvous_path(channel)

        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        record = json.loads(path.read_text())
        self.assertEqual(record["pid"], channel.proc.pid)
        self.assertFalse(record["bound"])
        self.assertFalse(record["verified"])
        self.assertIsNone(record["claude_session_id"])
        self.assertGreater(record["port"], 0)

        # The port is the whole point of the file, so prove it is answering.
        status, payload = get_local(self.bind_url(channel, "/status"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["pid"], channel.proc.pid)
        self.assertFalse(payload["bound"])

    def test_the_directory_is_0700_and_holds_no_token(self):
        channel = self.start()
        record = self.rendezvous(channel)
        self.assertEqual(stat.S_IMODE((self.home / "channels").stat().st_mode), 0o700)
        # The token is in credentials.json and belongs nowhere else.
        self.assertNotIn(DEVICE_TOKEN, json.dumps(record))

    def test_the_ancestry_starts_at_this_process_and_climbs(self):
        """R4's correlation mechanism is only as good as what this side records: the chain
        has to contain this process and reach the one that spawned it, which is how
        `connect` recognises the channel belonging to its own `claude`."""
        channel = self.start()
        ancestry = self.rendezvous(channel)["ancestry"]

        self.assertGreaterEqual(len(ancestry), 2)
        self.assertEqual(ancestry[0]["pid"], channel.proc.pid)
        self.assertEqual(ancestry[0]["ppid"], os.getpid())
        self.assertIn(os.getpid(), [row["pid"] for row in ancestry])

    def test_exiting_takes_the_file_with_it(self):
        channel = self.start()
        path = self.rendezvous_path(channel)
        channel.close()

        deadline = time.time() + WAIT
        while time.time() < deadline and path.exists():
            time.sleep(0.05)
        self.assertFalse(path.exists(), "a stale rendezvous file is how connect binds to a corpse")

    def test_a_dead_predecessors_file_is_pruned(self):
        """The abnormal exit — a `kill -9`, a crash — leaves a file behind. The next
        channel to start clears it out."""
        channels = self.home / "channels"
        channels.mkdir(parents=True, exist_ok=True)
        # A pid that cannot be running: allocate one and let it exit.
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        stale = channels / f"{dead.pid}.json"
        stale.write_text(json.dumps({"pid": dead.pid, "port": 1, "bound": True}))

        channel = self.start()
        self.rendezvous_path(channel)
        self.assertFalse(stale.exists())


# -- binding (R4) ------------------------------------------------------------------------


class TestBind(ChannelTestCase):
    def test_nothing_is_polled_before_a_bind(self):
        """The plugin's promise is that an unregistered session costs nothing. A channel
        server starts in *every* session where the plugin is enabled, so 'it polls only
        once bound' has to be true rather than intended."""
        self.start()
        time.sleep(3.0)
        self.assertEqual(self.backend.requests, [])

    def test_nothing_is_polled_until_the_handshake_is_answered(self):
        """A bind is not enough. Until the session proves it honours notifications, asking
        the backend for messages would only take them off the queue and drop them — and it
        is what put a handshake and an instruction in front of the model in one batch."""
        channel = self.start()
        self.bind(channel)
        channel.wait_for_event("handshake")

        time.sleep(3.0)
        self.assertEqual(self.backend.requests_to("/agent/inbound"), [])

    def test_a_verified_bind_polls_for_this_session(self):
        channel = self.start()
        status, payload = self.bind_and_verify(channel)

        self.assertEqual(status, 200)
        self.assertEqual(payload["claude_session_id"], CLAUDE_SESSION_ID)
        poll = self.wait_for_request("/agent/inbound")[0]
        self.assertEqual(poll["method"], "GET")
        self.assertEqual(poll["query"], {"session_id": AGENT_SESSION_ID})
        self.assertEqual(poll["authorization"], f"Bearer {DEVICE_TOKEN}")

        record = self.rendezvous(channel)
        self.assertTrue(record["bound"])
        self.assertEqual(record["claude_session_id"], CLAUDE_SESSION_ID)
        self.assertEqual(record["agent_session_id"], AGENT_SESSION_ID)

    def test_a_token_that_is_not_this_machines_is_refused(self):
        channel = self.start()
        status, payload = self.bind(channel, token="not-the-device-token")

        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "token_mismatch")
        time.sleep(1.0)
        self.assertEqual(self.backend.requests, [])
        self.assertFalse(self.rendezvous(channel)["bound"])

    def test_a_second_bind_is_refused_and_says_which_session_holds_it(self):
        channel = self.start()
        self.bind_and_verify(channel)
        status, payload = self.bind(channel, claude_session_id="99999999-9999-4999-8999-999999999999")

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "already_bound")
        self.assertEqual(payload["claude_session_id"], CLAUDE_SESSION_ID)
        self.assertFalse(payload["same_session"])

    def test_a_half_filled_bind_is_refused(self):
        channel = self.start()
        status, payload = post_local(self.bind_url(channel), {"token": DEVICE_TOKEN})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "missing_fields")

    def test_unbind_stops_the_polling(self):
        channel = self.start()
        self.bind_and_verify(channel)
        self.wait_for_request("/agent/inbound")

        status, payload = post_local(self.bind_url(channel, "/unbind"), {"token": DEVICE_TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(payload["claude_session_id"], CLAUDE_SESSION_ID)
        self.assertFalse(self.rendezvous(channel)["bound"])

        # The loop may be mid-tick when unbind lands, so allow one more poll and then
        # require silence across a full active interval.
        time.sleep(1.0)
        settled = len(self.backend.requests_to("/agent/inbound"))
        time.sleep(7.0)
        self.assertEqual(len(self.backend.requests_to("/agent/inbound")), settled)


# -- delivery (R5, R6) -------------------------------------------------------------------


class TestDelivery(ChannelTestCase):
    def test_an_instruction_arrives_as_one_channel_event_and_is_acked(self):
        self.backend.reply("/agent/inbound", 200, {"messages": [message("m-1", "change that back")]})
        channel = self.start()
        self.bind_and_verify(channel)

        event = channel.wait_for_event("instruction")[0]
        self.assertEqual(event["method"], "notifications/claude/channel")
        self.assertEqual(event["params"]["content"], "change that back")
        self.assertEqual(
            event["params"]["meta"],
            {"message_id": "m-1", "sender_name": "Mike", "sent_at": "2026-07-27T18:04:11Z"},
        )
        # Identifier-safe keys only: Claude Code drops a meta key with a hyphen in it
        # silently, and a missing attribute is not something you notice from here.
        for key in event["params"]["meta"]:
            self.assertRegex(key, r"^[A-Za-z_][A-Za-z0-9_]*$")

        ack = self.wait_for_request("/agent/inbound/ack")[0]
        self.assertEqual(ack["body"], {"message_ids": ["m-1"]})
        self.assertEqual(ack["authorization"], f"Bearer {DEVICE_TOKEN}")

    def test_the_ledger_is_written_before_the_ack(self):
        """R6's ordering, made visible: by the time the backend hears the ack, the id is
        already on disk — so a crash in that gap replays into a dedupe."""
        self.backend.reply("/agent/inbound", 200, {"messages": [message("m-1", "ship it")]})
        channel = self.start()
        self.bind_and_verify(channel)
        self.wait_for_request("/agent/inbound/ack")

        ledger = json.loads((self.home / "channels" / f"{CLAUDE_SESSION_ID}.delivered.json").read_text())
        self.assertEqual(ledger["message_ids"], ["m-1"])

    def test_a_message_served_twice_is_injected_once(self):
        """The at-least-once half of R6 within one process: the backend re-serving a
        message it never heard an ack for must not put it in front of Claude twice."""
        for _ in range(3):
            self.backend.reply("/agent/inbound", 200, {"messages": [message("m-1", "do the thing")]})
        channel = self.start()
        self.bind_and_verify(channel)

        self.wait_for_request("/agent/inbound", count=3)
        self.assertEqual(len(channel.channel_events("instruction")), 1)

    def test_dedupe_survives_a_restart(self):
        """And across processes, which is the case the on-disk ledger exists for: the user
        relaunches `claude`, connects again, and the backend still has an unacked message
        the previous process already put in front of them."""
        self.backend.reply("/agent/inbound", 200, {"messages": [message("m-1", "revert it")]})
        first = self.start()
        self.bind_and_verify(first)
        first.wait_for_event("instruction")
        first.close()

        # Same message again, as if the ack had never landed.
        self.backend.reply("/agent/inbound", 200, {"messages": [message("m-1", "revert it")]})
        second = self.start()
        acks_before = len(self.backend.requests_to("/agent/inbound/ack"))
        self.bind_and_verify(second)

        self.wait_for_request("/agent/inbound/ack", count=acks_before + 1)
        self.assertEqual(second.channel_events("instruction"), [])
        replayed = self.backend.requests_to("/agent/inbound/ack")[-1]
        self.assertEqual(replayed["body"], {"message_ids": ["m-1"]})

    def test_a_failed_ack_is_retried_on_the_next_poll(self):
        self.backend.reply("/agent/inbound", 200, {"messages": [message("m-1", "one more thing")]})
        self.backend.reply("/agent/inbound/ack", 500, {"error": "boom"})
        channel = self.start()
        self.bind_and_verify(channel)

        acks = self.wait_for_request("/agent/inbound/ack", count=2)
        self.assertEqual(acks[1]["body"], {"message_ids": ["m-1"]})
        # Still exactly one event in front of Claude, however many acks it took.
        self.assertEqual(len(channel.channel_events("instruction")), 1)

    def test_a_message_with_no_id_or_no_text_is_dropped(self):
        self.backend.reply(
            "/agent/inbound",
            200,
            {"messages": [{"id": "", "text": "no id"}, {"id": "m-2", "text": "   "}]},
        )
        channel = self.start()
        self.bind_and_verify(channel)

        self.wait_for_request("/agent/inbound", count=2)
        self.assertEqual(channel.channel_events("instruction"), [])


# -- the handshake (R10, R11) ------------------------------------------------------------


class TestHandshake(ChannelTestCase):
    def test_the_only_tool_is_the_ack(self):
        """R10. A reply tool would be a second, racing copy of the Stop hook, so there
        must not be one — this is the assertion that keeps it from growing back."""
        channel = self.start()
        tools = channel.request("tools/list")["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["audiochatty_ack"])

    def test_the_server_declares_itself_a_channel(self):
        channel = self.start()
        result = channel.init_result
        self.assertEqual(result["capabilities"]["experimental"], {"claude/channel": {}})
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "audiochatty")
        # The instructions are what tell Claude an inbound event is a prompt from the user.
        self.assertIn("audiochatty", result["instructions"])
        self.assertIn("audiochatty_ack", result["instructions"])

    def test_binding_probes_and_the_right_nonce_verifies(self):
        channel = self.start()
        self.bind(channel)

        probe = channel.wait_for_event("handshake")[0]
        nonce = probe["params"]["meta"]["nonce"]
        self.assertTrue(nonce)
        self.assertIn(nonce, probe["params"]["content"])

        result = channel.request("tools/call", {"name": "audiochatty_ack", "arguments": {"nonce": nonce}})
        self.assertEqual(result["result"]["content"][0]["text"], "verified")

        verified = self.wait_for_request("/agent/session/verified")[0]
        self.assertEqual(verified["body"], {"claude_session_id": CLAUDE_SESSION_ID})
        self.assertTrue(self.rendezvous(channel)["verified"])

    def test_a_wrong_nonce_verifies_nothing(self):
        channel = self.start()
        self.bind(channel)
        channel.wait_for_event("handshake")

        result = channel.request("tools/call", {"name": "audiochatty_ack", "arguments": {"nonce": "nope"}})
        self.assertEqual(result["result"]["content"][0]["text"], "ignored")
        time.sleep(1.0)
        self.assertEqual(self.backend.requests_to("/agent/session/verified"), [])
        self.assertFalse(self.rendezvous(channel)["verified"])

    def test_an_ack_before_any_bind_is_ignored(self):
        channel = self.start()
        result = channel.request("tools/call", {"name": "audiochatty_ack", "arguments": {"nonce": "x"}})
        self.assertEqual(result["result"]["content"][0]["text"], "ignored")

    def test_a_verification_the_backend_missed_is_retried(self):
        """The inbox reads `channel_verified_at`, so a verification the backend never heard
        about is a session the user is told they cannot talk to."""
        self.backend.reply("/agent/session/verified", 500, {"error": "boom"})
        channel = self.start()
        self.bind(channel)
        probe = channel.wait_for_event("handshake")[0]

        result = channel.request(
            "tools/call",
            {"name": "audiochatty_ack", "arguments": {"nonce": probe["params"]["meta"]["nonce"]}},
        )
        self.assertIn("will retry", result["result"]["content"][0]["text"])
        retried = self.wait_for_request("/agent/session/verified", count=2)
        self.assertEqual(retried[1]["body"], {"claude_session_id": CLAUDE_SESSION_ID})


# -- failure (the plugin's second rule) ---------------------------------------------------


class TestFailureIsSilent(ChannelTestCase):
    def test_a_dead_backend_costs_the_session_nothing(self):
        """The hooks' rule — a backend that is down or asleep is never felt in the
        terminal — applies here too, and here the failure lasts as long as the outage."""
        channel = self.start()
        # Refused instantly, so the test exercises the failure path rather than a timeout.
        status, _ = self.bind(channel, backend_url="http://127.0.0.1:1")
        self.assertEqual(status, 200)

        time.sleep(3.0)
        self.assertIsNone(channel.proc.poll(), "the channel process died with the backend")
        self.assertEqual(channel.channel_events("instruction"), [])
        # Still answering, still bound: the outage is invisible from the session's side.
        ok, payload = get_local(self.bind_url(channel, "/status"))
        self.assertEqual(ok, 200)
        self.assertTrue(payload["bound"])

    def test_stdout_is_only_mcp(self):
        """stdout *is* the transport. One stray `console.log` corrupts every message after
        it, and the symptom is a channel that silently stops working."""
        self.backend.reply("/agent/inbound", 200, {"messages": [message("m-1", "hello")]})
        channel = self.start(env_extra={"AUDIOCHATTY_DEBUG": "1"})
        self.bind_and_verify(channel)
        channel.wait_for_event("instruction")
        self.wait_for_request("/agent/inbound/ack")

        # `_read_loop` files anything unparseable under this key.
        self.assertEqual([n for n in channel.notifications if "__garbage__" in n], [])
        self.assertTrue(any("[audiochatty-channel]" in line for line in channel.stderr))


if __name__ == "__main__":
    unittest.main()
