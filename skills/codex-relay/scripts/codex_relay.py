#!/usr/bin/env python3
"""Drive Codex desktop app tasks from Claude Code.

A task is a Codex thread in the ChatGPT desktop app. Writes (start a turn, steer,
interrupt, change settings) go through the app's local IPC router as a thread
follower, the channel a second app window uses. Progress and outcomes are read
from the task's rollout file; the app's live snapshot fills in what the rollout
cannot show (pending approvals, commands still running, lost turns).

Every command prints JSON on stdout: one object, except `new`, which first prints a
line with the task id as soon as the task exists. Exit codes:
  0 ok   1 failed   2 invalid input   3 wrong state for this action
  4 app unavailable or incompatible   5 task not controlled by this session

See ../SKILL.md for the workflow and ../references/ipc-protocol.md for the protocol.
"""
import argparse
import calendar
import glob
import hashlib
import json
import os
import plistlib
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

RELAY_VERSION = "1.0.0"  # keep equal to metadata.version in ../SKILL.md
REPOSITORY_URL = "https://github.com/exgota/codex-relay"
LATEST_SKILL_URL = "https://raw.githubusercontent.com/exgota/codex-relay/main/skills/codex-relay/SKILL.md"
UPDATE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
UPDATE_INSTRUCTIONS = (
    "Tell the user a newer version exists and ask before updating. "
    "Claude Code plugin: /plugin marketplace update codex-relay, then /plugin update codex-relay@codex-relay. "
    "Skills CLI: npx skills update codex-relay. "
    "By hand: copy the new skills/codex-relay/ folder over the old one. "
    "To stop these checks, set CODEX_RELAY_NO_UPDATE_CHECK=1.")
TESTED_APP_VERSIONS = ("26.917.51856", "26.917.71314")
APP_BUNDLE_IDENTIFIER = "com.openai.codex"
# Per-method protocol versions this relay speaks. Checked against the app bundle's own
# table on first use of each app build (see check_protocol_compatibility).
METHOD_VERSIONS = {
    "thread-owner-discovery": 1,
    "thread-follower-start-turn": 2,
    "thread-follower-steer-turn": 1,
    "thread-follower-interrupt-turn": 4,  # 3 is used when no expectedTurnId is sent
    "thread-follower-load-complete-history": 1,
    "thread-follower-update-thread-settings": 2,
    "thread-stream-following-changed": 1,
    "thread-stream-state-changed": 11,
}
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
QUESTION_TOOL_NAMES = ("request_user_input", "request_user_input_async")
QUIET_SECONDS_BEFORE_APP_CHECK = 15
EXIT_OK, EXIT_FAILED, EXIT_INVALID, EXIT_WRONG_STATE, EXIT_APP, EXIT_NOT_PERMITTED = 0, 1, 2, 3, 4, 5


class RelayError(Exception):
    def __init__(self, message, kind="failed", exit_code=EXIT_FAILED, **details):
        super().__init__(message)
        self.kind, self.exit_code, self.details = kind, exit_code, details


# ---------- locations ----------

def codex_home():
    return os.path.abspath(os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"))


def socket_path():
    primary = os.path.join(codex_home(), "ipc", "ipc.sock")
    fallback = os.path.join(tempfile.gettempdir(), "codex-ipc", f"ipc-{os.getuid()}.sock")
    for path in (primary, fallback):
        if os.path.exists(path):
            return path
    return None


def state_directory():
    base = os.environ.get("CODEX_RELAY_STATE_DIR") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "codex-relay")
    os.makedirs(os.path.join(base, "tasks"), exist_ok=True)
    return base


def current_session():
    return os.environ.get("CODEX_RELAY_SESSION") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "terminal"


def find_app():
    candidates = [os.environ.get("CODEX_RELAY_APP"), "/Applications/ChatGPT.app",
                  os.path.expanduser("~/Applications/ChatGPT.app")]
    for candidate in candidates:
        if candidate and os.path.exists(os.path.join(candidate, "Contents", "Info.plist")):
            return candidate
    try:
        found = subprocess.run(["mdfind", f"kMDItemCFBundleIdentifier == '{APP_BUNDLE_IDENTIFIER}'"],
                               capture_output=True, text=True, timeout=10).stdout.split("\n")
        return next((path for path in found if path.endswith(".app")), None)
    except (OSError, subprocess.SubprocessError):
        return None


def app_version(app_path):
    with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as handle:
        return plistlib.load(handle).get("CFBundleShortVersionString")


def codex_binary():
    override = os.environ.get("CODEX_RELAY_CODEX_BIN")
    if override:
        return override
    on_path = shutil.which("codex")
    if on_path:
        return on_path
    app = find_app()
    bundled = app and os.path.join(app, "Contents", "Resources", "codex")
    if bundled and os.access(bundled, os.X_OK):
        return bundled
    raise RelayError("no codex binary: not on PATH and not inside the ChatGPT app bundle",
                     kind="app_unavailable", exit_code=EXIT_APP)


# ---------- protocol compatibility ----------

def parse_method_table(data):
    """Find the flat JSON object that maps method names to versions, whatever its key order."""
    marker = b'"thread-follower-steer-turn":'
    index = data.find(marker)
    while index != -1:
        start, end = data.rfind(b"{", 0, index), data.find(b"}", index)
        if start != -1 and end != -1:
            try:
                table = json.loads(data[start:end + 1])
                if isinstance(table, dict) and sum(isinstance(table.get(m), int) for m in METHOD_VERSIONS) >= 3:
                    return table
            except ValueError:
                pass
        index = data.find(marker, index + 1)
    return None


def bundle_method_versions(app_path):
    """Read the app's own method-version table out of app.asar."""
    asar = os.path.join(app_path, "Contents", "Resources", "app.asar")
    with open(asar, "rb") as handle:
        chunk_size, overlap, position = 1 << 24, 1 << 16, 0
        while True:
            handle.seek(position)
            data = handle.read(chunk_size)
            if not data:
                return None
            table = parse_method_table(data)
            if table is not None:
                return table
            position += chunk_size - overlap


def check_protocol_compatibility(force=False):
    """Fail clearly when the app build speaks different method versions. The bundle is
    scanned once per app build; the verdict is cached in the relay state directory."""
    app = find_app()
    if not app:
        raise RelayError("the ChatGPT desktop app was not found", kind="app_unavailable", exit_code=EXIT_APP)
    version = app_version(app)
    cache_path = os.path.join(state_directory(), "compatibility.json")
    try:
        cache = json.load(open(cache_path))
    except (OSError, ValueError):
        cache = {}
    expected = json.loads(os.environ.get("CODEX_RELAY_EXPECTED_VERSIONS_TEST") or "null") or METHOD_VERSIONS
    key = f"{version}|{json.dumps(expected, sort_keys=True)}"
    if not force and cache.get("key") == key:
        verdict = cache["verdict"]
    else:
        table = bundle_method_versions(app)
        if table is None:
            verdict = {"compatible": False, "reason": "method-version table not found in app.asar"}
        else:
            mismatched = {method: {"relay": wanted, "app": table.get(method)}
                          for method, wanted in expected.items() if table.get(method) != wanted}
            verdict = {"compatible": not mismatched, "mismatched": mismatched}
        json.dump({"key": key, "verdict": verdict}, open(cache_path, "w"))
    verdict = dict(verdict, app_version=version, tested=version in TESTED_APP_VERSIONS)
    if not verdict["compatible"]:
        raise RelayError(f"ChatGPT app {version} speaks a different relay protocol; see references/ipc-protocol.md, "
                         "section 'After an app update'", kind="incompatible_app", exit_code=EXIT_APP, **verdict)
    return verdict


# ---------- IPC ----------

class IpcConnection:
    """A client of the app's IPC router: 4-byte little-endian length + UTF-8 JSON."""

    def __init__(self):
        path = socket_path()
        if not path:
            raise RelayError(f"the ChatGPT app is not running (no IPC socket under {codex_home()})",
                             kind="app_unavailable", exit_code=EXIT_APP)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.socket.connect(path)
        except OSError as error:
            raise RelayError(f"cannot connect to the app's IPC socket: {error}", kind="app_unavailable",
                             exit_code=EXIT_APP)
        self.buffer = b""
        self.broadcasts = []
        self.client_identifier = "initializing-client"
        response = self.request("initialize", {"clientType": "codex-relay"}, timeout=5, version=0)
        result = response.get("result") or {}
        if response.get("resultType") != "success" or "clientId" not in result:
            raise RelayError(f"unexpected IPC handshake response: {json.dumps(response)[:300]}",
                             kind="incompatible_app", exit_code=EXIT_APP)
        self.client_identifier = result["clientId"]

    def close(self):
        try:
            self.socket.close()
        except OSError:
            pass

    def _write(self, message):
        data = json.dumps(message).encode("utf-8")
        self.socket.sendall(struct.pack("<I", len(data)) + data)

    def messages(self, deadline):
        """Yield messages until the deadline. Router discovery probes are answered with
        canHandle false so other clients are never kept waiting."""
        while True:
            while len(self.buffer) >= 4:
                length = struct.unpack("<I", self.buffer[:4])[0]
                if len(self.buffer) < 4 + length:
                    break
                message = json.loads(self.buffer[4:4 + length])
                self.buffer = self.buffer[4 + length:]
                if message.get("type") == "client-discovery-request":
                    self._write({"type": "client-discovery-response", "requestId": message["requestId"],
                                 "response": {"canHandle": False}})
                    continue
                yield message
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            readable, _, _ = select.select([self.socket], [], [], remaining)
            if not readable:
                return
            chunk = self.socket.recv(1 << 22)
            if not chunk:
                raise RelayError("the app closed the IPC connection", kind="disconnected", exit_code=EXIT_APP)
            self.buffer += chunk

    def request(self, method, params, timeout=30, target=None, version=None):
        request_identifier = str(uuid.uuid4())
        message = {"type": "request", "requestId": request_identifier, "sourceClientId": self.client_identifier,
                   "version": METHOD_VERSIONS.get(method, 0) if version is None else version,
                   "method": method, "params": params, "timeoutMs": int(timeout * 1000)}
        if target:
            message["targetClientId"] = target
        self._write(message)
        for received in self.messages(time.time() + timeout + 2):
            if received.get("type") == "response" and received.get("requestId") == request_identifier:
                return received
            self.broadcasts.append(received)
        return {"type": "response", "resultType": "error", "error": "relay-timeout"}

    def broadcast(self, method, params):
        self._write({"type": "broadcast", "method": method, "sourceClientId": self.client_identifier,
                     "version": METHOD_VERSIONS.get(method, 0), "params": params})


def classify_ipc_error(error):
    error = str(error or "")
    if "version-mismatch" in error:
        return "incompatible_app"
    if error == "relay-timeout" or error.endswith("-timeout") or "request-timeout" in error:
        return "timeout"
    if error.startswith("no-client-found") or "client-disconnected" in error:
        return "owner_unavailable"
    return "rejected"


def find_owner(connection, task, open_if_missing, background=True, timeout=30):
    """Return the IPC client id of the app window that owns the task, or None. The app
    only owns a thread it has loaded; a deep link loads it."""
    deadline, opened = time.time() + timeout, False
    while time.time() < deadline:
        response = connection.request("thread-owner-discovery", {"hostId": "local", "conversationId": task},
                                      timeout=12)
        if response.get("resultType") == "success":
            return response.get("handledByClientId")
        kind = classify_ipc_error(response.get("error"))
        if kind == "incompatible_app":
            raise RelayError(f"owner discovery rejected: {response.get('error')}", kind=kind, exit_code=EXIT_APP)
        if not open_if_missing:
            return None
        if not opened:
            open_in_app(task, background)
            opened = True
        time.sleep(1.5)
    raise RelayError(f"the app did not load task {task} within {timeout}s", kind="owner_unavailable",
                     exit_code=EXIT_APP)


def follower_request(connection, owner, method, params, timeout=20, version=None):
    response = connection.request(method, params, timeout=timeout, target=owner, version=version)
    if response.get("resultType") == "success":
        return {"ok": True, "result": response.get("result")}
    return {"ok": False, "error": response.get("error"), "kind": classify_ipc_error(response.get("error"))}


def app_snapshot(connection, owner, task, timeout=20):
    """Full live conversation state from the owning window, or None."""
    def is_snapshot(message):
        params = message.get("params") or {}
        return (message.get("method") == "thread-stream-state-changed" and params.get("conversationId") == task
                and (params.get("change") or {}).get("type") == "snapshot")
    connection.broadcasts = [message for message in connection.broadcasts if not is_snapshot(message)]
    connection.broadcast("thread-stream-following-changed",
                         {"conversationId": task, "hostId": "local", "following": True})
    try:
        reply = follower_request(connection, owner, "thread-follower-load-complete-history",
                                 {"conversationId": task}, timeout=timeout)
        if not reply["ok"]:
            return None
        for message in list(connection.broadcasts):
            if is_snapshot(message):
                connection.broadcasts.remove(message)
                return message["params"]["change"].get("conversationState")
        for message in connection.messages(time.time() + timeout):
            if is_snapshot(message):
                return message["params"]["change"].get("conversationState")
            connection.broadcasts.append(message)
        return None
    finally:
        connection.broadcast("thread-stream-following-changed",
                             {"conversationId": task, "hostId": "local", "following": False})


def summarize_snapshot(state):
    history = ((state.get("turnHistory") or {}).get("history") or {})
    entities = history.get("entitiesByKey") or {}
    order = [entry.get("value") for island in history.get("islands") or [] for entry in island.get("entries") or []]
    turns = [entities[key] for key in order if key in entities] or state.get("turns") or []
    last = turns[-1] if turns else {}
    running = []
    for item in last.get("items") or []:
        kind = str(item.get("type", ""))
        if "command" in kind.lower() and item.get("status") in ("inProgress", "in_progress", "running"):
            command = item.get("command")
            running.append(" ".join(command) if isinstance(command, list) else str(command))
    runtime = state.get("threadRuntimeStatus") or {}
    settings = state.get("latestThreadSettings") or {}
    return {"runtime": runtime.get("type"), "flags": runtime.get("activeFlags") or [],
            "turn_id": last.get("turnId"), "turn_status": last.get("status"), "turn_error": last.get("error"),
            "running_commands": running, "pending_requests": len(state.get("requests") or []),
            "model": settings.get("model") or state.get("latestModel"),
            "effort": settings.get("effort") or state.get("latestReasoningEffort")}


def open_in_app(task, background=False):
    subprocess.run(["open"] + (["-g"] if background else []) + [f"codex://threads/{task}"], check=False)


# ---------- task records (no prompt text is stored) ----------

def validate_task(task):
    if not UUID_PATTERN.match(task or ""):
        raise RelayError(f"'{task}' is not a complete task id (36-character UUID)", kind="invalid_task",
                         exit_code=EXIT_INVALID)
    return task


def record_path(task):
    return os.path.join(state_directory(), "tasks", f"{task}.json")


def load_record(task):
    try:
        return json.load(open(record_path(task)))
    except (OSError, ValueError):
        return None


def save_record(record):
    record["updated_at"] = time.time()
    temporary = record_path(record["task"]) + ".tmp"
    with open(temporary, "w") as handle:
        json.dump(record, handle, indent=1)
    os.replace(temporary, record_path(record["task"]))


def require_control(task):
    record = load_record(task)
    if record is None:
        find_rollout(task)  # an unknown id is reported as unknown, not as a permission problem
        raise RelayError("this relay has no record of that task. Control is limited to tasks created with `new` "
                         "or adopted with `adopt` after the user named them.", kind="not_permitted",
                         exit_code=EXIT_NOT_PERMITTED)
    if record.get("session") != current_session():
        owner = record.get("session")
        raise RelayError((f"task belongs to another Claude session ({owner})." if owner else
                          "no session is recorded for this task.") +
                         " Adopt it only if the user named this task in this session.",
                         kind="not_permitted", exit_code=EXIT_NOT_PERMITTED)
    return record


def text_digest(text):
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


# ---------- rollout reading ----------

def find_rollout(task, record=None):
    cached = (record or {}).get("rollout")
    if cached and cached.startswith(codex_home() + os.sep) and os.path.exists(cached):
        return cached
    pattern = f"rollout-*-{task}.jsonl"
    matches = glob.glob(os.path.join(codex_home(), "sessions", "*", "*", "*", pattern))
    if matches:
        path = max(matches, key=os.path.getmtime)
        if record is not None:
            record["rollout"] = path
        return path
    if glob.glob(os.path.join(codex_home(), "archived_sessions", "**", pattern), recursive=True):
        raise RelayError(f"task {task} is archived", kind="archived", exit_code=EXIT_WRONG_STATE)
    raise RelayError(f"no task {task} under {codex_home()}", kind="unknown_task", exit_code=EXIT_INVALID)


def read_events(path, offset=0):
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read()
    complete = data[:data.rfind(b"\n") + 1]  # never parse a half-written last line
    events = []
    for line in complete.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events, offset + len(complete)


def message_of(event):
    payload = event.get("payload") or {}
    if event.get("type") == "event_msg" and payload.get("type") == "item_completed":
        item = payload.get("item") or {}
        if item.get("type") in ("UserMessage", "AgentMessage"):
            text = "\n".join(part.get("text", "") for part in item.get("content") or [] if isinstance(part, dict))
            return ("user" if item["type"] == "UserMessage" else "agent"), text
    return None


class TaskTracker:
    """Incremental rollout state for the task's current turn."""

    def __init__(self):
        self.turn_id = None
        self.turn_state = None  # working | completed | interrupted | failed
        self.turn_error = None
        self.pending_calls = {}
        self.questions = {}
        self.last_agent_message = None
        self.final_message = None
        self.model = self.effort = None
        self.last_event_at = None
        self.offset = 0

    def feed(self, path, offset=None):
        events, end = read_events(path, self.offset if offset is None else offset)
        for event in events:
            self.last_event_at = event.get("timestamp") or self.last_event_at
            payload = event.get("payload") or {}
            kind = payload.get("type")
            if event.get("type") == "turn_context":
                self.model, self.effort = payload.get("model"), payload.get("effort")
            elif kind == "task_started":
                self.turn_id, self.turn_state, self.turn_error = payload.get("turn_id"), "working", None
                self.pending_calls, self.questions, self.final_message = {}, {}, None
            elif kind == "task_complete" and payload.get("turn_id") == self.turn_id:
                self.turn_state = "failed" if payload.get("error") else "completed"
                self.turn_error = payload.get("error")
                self.final_message = payload.get("last_agent_message")
            elif kind == "turn_aborted" and payload.get("turn_id") == self.turn_id:
                self.turn_state = "interrupted" if payload.get("reason") == "interrupted" else "failed"
                self.turn_error = None if self.turn_state == "interrupted" else {"reason": payload.get("reason")}
            elif kind in ("function_call", "custom_tool_call"):
                self.pending_calls[payload.get("call_id")] = payload.get("name")
                if payload.get("name") in QUESTION_TOOL_NAMES:
                    self.questions[payload.get("call_id")] = payload.get("arguments") or payload.get("input")
            elif kind in ("function_call_output", "custom_tool_call_output"):
                self.pending_calls.pop(payload.get("call_id"), None)
                self.questions.pop(payload.get("call_id"), None)
            found = message_of(event)
            if found and found[0] == "agent":
                self.last_agent_message = found[1]
        self.offset = end
        return events

    def pending_questions(self):
        """The questions Codex is waiting on, as asked (request_user_input arguments)."""
        found = []
        for raw in self.questions.values():
            try:
                arguments = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except ValueError:
                found.append({"question": str(raw)})
                continue
            if not isinstance(arguments, dict):
                found.append({"question": str(arguments)})
                continue
            questions = arguments.get("questions")
            for question in (questions if isinstance(questions, list) else [questions or arguments]):
                if not isinstance(question, dict):
                    found.append({"question": str(question)})
                    continue
                entry = {"question": question.get("question") or question.get("prompt") or question.get("header")}
                options = question.get("options")
                if options:
                    entry["options"] = [option.get("label", option) if isinstance(option, dict) else option
                                        for option in options]
                found.append(entry)
        return found

    def state(self):
        if self.turn_state == "working" and any(name in QUESTION_TOOL_NAMES for name in self.pending_calls.values()):
            return "waiting_for_input"
        return self.turn_state or "idle"

    def seconds_since_activity(self):
        if not self.last_event_at:
            return None
        try:
            stamp = time.strptime(self.last_event_at[:19], "%Y-%m-%dT%H:%M:%S")
            return round(time.time() - calendar.timegm(stamp), 1)
        except ValueError:
            return None


def track_from(path, offset):
    tracker = TaskTracker()
    tracker.offset = offset
    tracker.feed(path)
    return tracker


def track_whole(path):
    tracker = TaskTracker()
    tracker.feed(path, 0)
    return tracker


def digest_seen_after(path, offset, digest):
    events, _ = read_events(path, offset)
    return any((found := message_of(event)) and found[0] == "user" and text_digest(found[1]) == digest
               for event in events)


def wait_for_rollout(path, offset, predicate, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        tracker = track_from(path, offset)
        if predicate(tracker):
            return tracker
        time.sleep(0.5)
    return None


# ---------- combined task inspection ----------

def inspect(task, record=None, use_app=True):
    """Best available state: rollout first, then the app's live snapshot."""
    path = find_rollout(task, record)
    tracker = track_whole(path)
    result = {"task": task, "state": tracker.state(), "turn_id": tracker.turn_id,
              "model": tracker.model, "effort": tracker.effort,
              "last_activity_seconds": tracker.seconds_since_activity(),
              "final_message": tracker.final_message if tracker.state() in ("completed", "failed") else None,
              "error": tracker.turn_error, "source": "rollout", "limits": []}
    if tracker.state() == "waiting_for_input":
        result["questions"] = tracker.pending_questions()
    if result["state"] not in ("working", "waiting_for_input"):
        return result, tracker
    if not use_app:
        result["limits"].append("app not consulted: approval requests and running commands are not visible")
        return result, tracker
    if not socket_path():
        result.update(state="disconnected", detail="the ChatGPT app is not running; this turn cannot finish")
        return result, tracker
    connection = IpcConnection()
    try:
        owner = find_owner(connection, task, open_if_missing=False)
        if owner is None:
            result.update(state="disconnected",
                          detail="the rollout shows an open turn but no app window is running this task "
                                 "(the app may have restarted, or setup was cut off); the turn will not finish. "
                                 "`send` opens the task in the app and starts a new turn.")
            return result, tracker
        snapshot = app_snapshot(connection, owner, task)
    finally:
        connection.close()
    if snapshot is None:
        result["limits"].append("the app did not return a snapshot; state is from the rollout only")
        return result, tracker
    summary = summarize_snapshot(snapshot)
    result["source"] = "rollout+app"
    result["running_commands"] = summary["running_commands"]
    result["pending_requests"] = summary["pending_requests"]
    if summary["runtime"] == "active":
        if "waitingOnApproval" in summary["flags"]:
            result["state"] = "waiting_for_approval"
        elif "waitingOnUserInput" in summary["flags"]:
            result["state"] = "waiting_for_input"
        elif result["state"] not in ("waiting_for_input",):
            result["state"] = "working"
    elif summary["runtime"] == "systemError":
        result.update(state="failed", detail="the app reports a system error for this task")
    elif summary["runtime"] == "idle" and result["state"] in ("working", "waiting_for_input"):
        # The rollout shows an open turn but the app runs nothing: either the rollout lags
        # a final status, or the turn was cut off (app restart, killed setup) and is dead.
        mapping = {"completed": "completed", "interrupted": "interrupted", "failed": "failed"}
        if summary["turn_status"] in mapping:
            result["state"] = mapping[summary["turn_status"]]
        else:
            result.update(state="lost", detail="the app has no active turn for this task; the last turn was cut "
                                               "off and will not finish. `send` starts a new turn.")
    return result, tracker


# ---------- turn operations ----------

def build_input(text, image_paths):
    items = [{"type": "text", "text": text, "text_elements": []}]
    for path in image_paths or []:
        if not os.path.exists(path):
            raise RelayError(f"image not found: {path}", kind="invalid_input", exit_code=EXIT_INVALID)
        items.append({"type": "localImage", "path": os.path.abspath(path)})
    return items


def apply_settings(connection, owner, task, record, tracker, model, effort):
    """Set model/effort for the next turn the way the app's own composer does."""
    if effort and effort not in EFFORTS:
        raise RelayError(f"unknown effort '{effort}'; use one of {', '.join(EFFORTS)}", kind="invalid_input",
                         exit_code=EXIT_INVALID)
    current = summarize_snapshot(app_snapshot(connection, owner, task) or {}) if not (model and effort) else {}
    settings = {"model": model or current.get("model") or tracker.model,
                "effort": effort or current.get("effort") or tracker.effort}
    if not settings["model"]:
        raise RelayError("this task has no recorded model yet; pass --model together with --effort",
                         kind="invalid_input", exit_code=EXIT_INVALID)
    reply = follower_request(connection, owner, "thread-follower-update-thread-settings",
                             {"conversationId": task, "threadSettings": settings, "activeTurnId": None,
                              "condition": None})
    if not reply["ok"] or not (reply["result"] or {}).get("applied"):
        raise RelayError(f"the app did not apply settings {settings}: {reply.get('error') or reply.get('result')}",
                         kind="settings_not_applied")
    record["requested"] = settings
    return settings


def verify_turn_settings(connection, owner, task, record, tracker, requested):
    """Stop the turn at once if it is not running with the requested model/effort."""
    actual = {"model": tracker.model, "effort": tracker.effort}
    wrong = {key: {"requested": value, "actual": actual[key]} for key, value in (requested or {}).items()
             if value and actual[key] != value}
    if wrong:
        follower_request(connection, owner, "thread-follower-interrupt-turn",
                         {"conversationId": task, "mode": "user-stop"}, timeout=10, version=3)
        record["last_operation"]["outcome"] = "stopped_settings_mismatch"
        save_record(record)
        raise RelayError("the turn started with different settings than requested, so it was interrupted",
                         kind="settings_mismatch", exit_code=EXIT_FAILED, mismatch=wrong)
    return actual


def start_turn(connection, owner, task, record, text, image_paths, model=None, effort=None, background=True):
    path = find_rollout(task, record)
    before = track_whole(path)
    requested = None
    if model or effort:
        requested = apply_settings(connection, owner, task, record, before, model, effort)
    turn_input = build_input(text, image_paths)
    offset = os.path.getsize(path)
    record["last_operation"] = {"kind": "start", "at": time.time(), "rollout_offset": offset,
                                "prompt_sha256": text_digest(text), "outcome": "sent"}
    record["wait_cursor"] = offset
    save_record(record)
    timeout = float(os.environ.get("CODEX_RELAY_START_TIMEOUT_TEST") or 20)
    reply = follower_request(connection, owner, "thread-follower-start-turn",
                             {"conversationId": task,
                              "turnStart": {"request": {"threadId": task, "input": turn_input},
                                            "context": {}}}, timeout=timeout)
    delivered = reply["ok"] or reply.get("kind") == "timeout"
    started = wait_for_rollout(path, offset, lambda t: t.turn_id is not None, timeout=30 if delivered else 3)
    if started is None:
        record["last_operation"]["outcome"] = "unconfirmed" if delivered else "rejected"
        save_record(record)
        if not delivered:
            raise RelayError(f"the app rejected the turn: {reply.get('error')}", kind=reply.get("kind"),
                             exit_code=EXIT_APP if reply.get("kind") == "incompatible_app" else EXIT_FAILED)
        raise RelayError("the turn was sent but has not appeared. Do not resend: run `status` first.",
                         kind="unconfirmed", task=task)
    record["last_operation"].update(outcome="started" if reply["ok"] else "started_after_timeout",
                                    turn_id=started.turn_id)
    if record.get("stage") in ("bootstrapping", "bootstrap_timeout", "abandoned"):
        record["stage"] = "ready"
    # turn_context (the turn's actual model and effort) can trail task_started by tens of seconds.
    with_context = wait_for_rollout(path, offset, lambda t: t.turn_id is not None and t.model is not None,
                                    timeout=90)
    if with_context is None:
        save_record(record)
        return {"task": task, "action": "started", "turn_id": started.turn_id, "model": None, "effort": None,
                "outcome": record["last_operation"]["outcome"], "settings_verified": False,
                "detail": "the turn started but has not recorded its model and effort yet; `status` shows them "
                          "once written" + ("; compare them with the request" if requested else "")}
    started = with_context
    if record.get("stage") in ("bootstrapping", "bootstrap_timeout", "abandoned"):
        record["stage"] = "ready"
    save_record(record)
    actual = verify_turn_settings(connection, owner, task, record, started, requested)
    return {"task": task, "action": "started", "turn_id": started.turn_id, "model": actual["model"],
            "effort": actual["effort"], "outcome": record["last_operation"]["outcome"], "settings_verified": True}


# ---------- update check (notify and ask; never installs anything) ----------

def version_tuple(text):
    match = re.search(r"^\s+version:\s*[\"']?(\d+(?:\.\d+)*)", text or "", re.M)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def local_version():
    try:
        skill_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "SKILL.md")
        return version_tuple(open(skill_file, encoding="utf-8").read())
    except OSError:
        return None


def update_available():
    """At most once a day, compare this copy's version with the latest on GitHub. Any
    failure is silent: the check must never break or noticeably slow a relay command."""
    if os.environ.get("CODEX_RELAY_NO_UPDATE_CHECK"):
        return None
    cache_directory = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                                   "codex-relay")
    cache_path = os.path.join(cache_directory, "update_check.json")
    try:
        cache = json.load(open(cache_path))
    except (OSError, ValueError):
        cache = {}
    if not isinstance(cache, dict):
        cache = {}
    try:
        checked_at = float(cache.get("checked_at") or 0)
    except (TypeError, ValueError):
        checked_at = 0
    latest = cache.get("latest")
    if time.time() - checked_at >= UPDATE_CHECK_INTERVAL_SECONDS:
        latest = None
        try:
            url = os.environ.get("CODEX_RELAY_UPDATE_URL") or LATEST_SKILL_URL
            with urllib.request.urlopen(url, timeout=3) as response:
                found = version_tuple(response.read(65536).decode("utf-8", "replace"))
            latest = ".".join(map(str, found)) if found else None
        except Exception:
            latest = None
        try:
            os.makedirs(cache_directory, exist_ok=True)
            json.dump({"checked_at": time.time(), "latest": latest}, open(cache_path, "w"))
        except OSError:
            pass
    current = local_version()
    try:
        newer = latest and current and tuple(int(part) for part in str(latest).split(".")) > current
    except ValueError:
        newer = False
    if not newer:
        return None
    return {"current": ".".join(map(str, current)), "latest": latest, "repository": REPOSITORY_URL,
            "instructions": UPDATE_INSTRUCTIONS}


# ---------- commands ----------

def emit(payload, exit_code=EXIT_OK):
    if isinstance(payload, dict) and "update_available" not in payload:
        try:
            notice = update_available()
        except Exception:
            notice = None
        if notice:
            payload["update_available"] = notice
    print(json.dumps(payload, ensure_ascii=False))
    return exit_code


def validate_images(arguments):
    for path in getattr(arguments, "image", None) or []:
        if not os.path.isfile(path):
            raise RelayError(f"image not found: {path}", kind="invalid_input", exit_code=EXIT_INVALID)


def read_prompt(arguments):
    if arguments.prompt_file:
        try:
            text = open(arguments.prompt_file, encoding="utf-8").read()
        except OSError as error:
            raise RelayError(f"cannot read prompt file: {error}", kind="invalid_input", exit_code=EXIT_INVALID)
    else:
        text = arguments.prompt or ""
    if not text.strip():
        raise RelayError("the prompt is empty", kind="invalid_input", exit_code=EXIT_INVALID)
    validate_images(arguments)
    return text


def remaining(arguments, started):
    return max(5.0, arguments.timeout - (time.time() - started))


def command_new(arguments):
    command_started = time.time()
    text = read_prompt(arguments)
    if arguments.effort and arguments.effort not in EFFORTS:
        raise RelayError(f"unknown effort '{arguments.effort}'", kind="invalid_input", exit_code=EXIT_INVALID)
    working_directory = os.path.abspath(os.path.expanduser(arguments.cwd))
    if not os.path.isdir(working_directory):
        raise RelayError(f"no such directory: {working_directory}", kind="invalid_input", exit_code=EXIT_INVALID)
    check_protocol_compatibility()
    IpcConnection().close()  # the app must be up before anything is created
    binary = codex_binary()
    title = " ".join((arguments.title or f"codex-relay task {time.strftime('%m-%d %H:%M')}").split())[:80]
    marker = uuid.uuid4().hex[:12]
    # The app can only open a thread that already has a turn on disk, so the thread is
    # created with one minimal headless turn carrying the requested model and effort. The
    # brief never goes into it: this turn runs outside the app, unwatched.
    bootstrap = (f"{title}\n\n(codex-relay setup {marker}. The line above is only a label for the sidebar; "
                 "do not act on it. Reply with one word: ready.)")
    command = [binary, "exec", "--json", "--skip-git-repo-check", "-C", working_directory]
    if arguments.model:
        command += ["-m", arguments.model]
    if arguments.effort:
        command += ["-c", f'model_reasoning_effort="{arguments.effort}"']
    started_at = time.time()
    error_log = tempfile.TemporaryFile(mode="w+")
    process = subprocess.Popen(command + [bootstrap], stdout=subprocess.PIPE, stderr=error_log,
                               stdin=subprocess.DEVNULL, text=True)
    task, deadline = None, started_at + arguments.bootstrap_timeout
    while time.time() < deadline:
        readable, _, _ = select.select([process.stdout], [], [], max(0.1, deadline - time.time()))
        if not readable:
            continue
        line = process.stdout.readline()
        if not line:
            try:
                process.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                pass
            break
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "thread.started" and not task:
            task = event.get("thread_id")
            save_record({"task": task, "session": current_session(), "origin": "created", "created_at": started_at,
                         "cwd": working_directory, "stage": "bootstrapping"})
            print(json.dumps({"task": task, "stage": "created"}), flush=True)
    if process.poll() is None:
        process.kill()
        process.wait()
        written = False
        if task:
            record = load_record(task)
            try:
                find_rollout(task, record)
                written = True
            except RelayError:
                pass
            record["stage"] = "bootstrap_timeout" if written else "abandoned"
            save_record(record)
        if written:
            advice = f"; task {task} was written to disk, so inspect it with `status` instead of creating another"
        else:
            advice = "; nothing was written to disk, so running `new` again is safe"
        raise RelayError(f"thread setup did not finish in {arguments.bootstrap_timeout:g}s" + advice,
                         kind="bootstrap_timeout", task=task, written=written)
    if not task:
        error_log.seek(0)
        raise RelayError(f"codex exec did not create a thread: {error_log.read()[-800:]}",
                         kind="bootstrap_failed")
    record = load_record(task)
    record["stage"] = "ready"
    path = find_rollout(task, record)
    bootstrap_state = track_whole(path)
    if arguments.effort and bootstrap_state.effort != arguments.effort:
        save_record(record)
        raise RelayError("thread setup ran with a different effort than requested", kind="settings_mismatch",
                         mismatch={"requested": arguments.effort, "actual": bootstrap_state.effort}, task=task)
    save_record(record)
    connection = IpcConnection()
    try:
        owner = find_owner(connection, task, open_if_missing=True, background=arguments.background)
        result = start_turn(connection, owner, task, record, text, arguments.image,
                            arguments.model, arguments.effort, arguments.background)
    finally:
        connection.close()
    if arguments.wait:
        result["wait"] = wait_loop(task, record, remaining(arguments, command_started), arguments.progress)
    return emit(result)


def command_adopt(arguments):
    task = validate_task(arguments.task)
    record = load_record(task) or {"task": task, "created_at": None, "origin": "adopted"}
    find_rollout(task, record)
    previous = record.get("session")
    record.update(session=current_session(), adopted_at=time.time(), origin=record.get("origin", "adopted"))
    save_record(record)
    return emit({"task": task, "action": "adopted", "previous_session": previous})


def command_open(arguments):
    task = validate_task(arguments.task)
    find_rollout(task, load_record(task))
    open_in_app(task, arguments.background)
    return emit({"task": task, "action": "opened"})


def command_send(arguments):
    command_started = time.time()
    task = validate_task(arguments.task)
    record = require_control(task)
    text = read_prompt(arguments)
    state, tracker = inspect(task, record)
    if state["state"] in ("working", "waiting_for_input", "waiting_for_approval"):
        raise RelayError(f"the task is {state['state']}; `send` only starts a new turn. Use `steer` to change "
                         "the running turn, or `wait`.", kind="wrong_state", exit_code=EXIT_WRONG_STATE, state=state)
    if state["state"] == "disconnected" and socket_path():
        # Load the task in the app, then ask again: a dead turn shows up as `lost`.
        connection = IpcConnection()
        try:
            find_owner(connection, task, open_if_missing=True, background=arguments.background)
        finally:
            connection.close()
        state, tracker = inspect(task, record)
    if state["state"] == "disconnected":
        raise RelayError(state.get("detail", "disconnected"), kind="disconnected", exit_code=EXIT_APP)
    operation = record.get("last_operation") or {}
    if (not arguments.allow_repeat and operation.get("prompt_sha256") == text_digest(text)
            and digest_seen_after(find_rollout(task, record), operation.get("rollout_offset", 0), text_digest(text))):
        raise RelayError("this exact prompt was already delivered to the task. Pass --allow-repeat to send it again.",
                         kind="duplicate", exit_code=EXIT_WRONG_STATE)
    connection = IpcConnection()
    try:
        owner = find_owner(connection, task, open_if_missing=True, background=arguments.background)
        result = start_turn(connection, owner, task, record, text, arguments.image,
                            arguments.model, arguments.effort, arguments.background)
    finally:
        connection.close()
    if arguments.wait:
        result["wait"] = wait_loop(task, record, remaining(arguments, command_started), arguments.progress)
    return emit(result)


def command_steer(arguments):
    command_started = time.time()
    task = validate_task(arguments.task)
    record = require_control(task)
    text = read_prompt(arguments)
    state, tracker = inspect(task, record)
    if state["state"] != "working":
        advice = {"waiting_for_input": "Codex asked a question; the user answers it in the app.",
                  "waiting_for_approval": "Codex is waiting for an approval; the user decides in the app."}
        raise RelayError(f"the task is {state['state']}, not working. " +
                         advice.get(state["state"], "Use `send` to start a new turn."),
                         kind="wrong_state", exit_code=EXIT_WRONG_STATE, state=state)
    path = find_rollout(task, record)
    offset = os.path.getsize(path)
    record["last_operation"] = {"kind": "steer", "at": time.time(), "rollout_offset": offset,
                                "prompt_sha256": text_digest(text), "outcome": "sent"}
    save_record(record)
    working_directory = record.get("cwd") or os.path.expanduser("~")
    connection = IpcConnection()
    try:
        owner = find_owner(connection, task, open_if_missing=True, background=arguments.background)
        reply = follower_request(connection, owner, "thread-follower-steer-turn", {
            "conversationId": task, "input": build_input(text, arguments.image),
            "restoreMessage": {"text": text, "cwd": working_directory,
                               "context": {"workspaceRoots": [working_directory], "commentAttachments": []},
                               "responsesapiClientMetadata": {}},
            "serviceTier": None, "attachments": [], "clientUserMessageId": str(uuid.uuid4()),
            "additionalContext": None, "toolOutput": None})
    finally:
        connection.close()
    if not reply["ok"] and "no active turn" in str(reply.get("error")):
        record["last_operation"]["outcome"] = "rejected_turn_ended"
        save_record(record)
        raise RelayError("the turn ended before the steer arrived; use `send` for a new turn",
                         kind="wrong_state", exit_code=EXIT_WRONG_STATE)
    delivered = wait_for_rollout(path, offset, lambda t: digest_seen_after(path, offset, text_digest(text)),
                                 timeout=20 if reply["ok"] or reply.get("kind") == "timeout" else 2)
    if delivered:
        outcome = "delivered" if reply["ok"] else "delivered_after_timeout"
    elif reply["ok"]:
        # The app accepted it; Codex reads it at its next step, which a running command can delay.
        outcome = "accepted"
    else:
        record["last_operation"]["outcome"] = "unconfirmed" if reply.get("kind") == "timeout" else "rejected"
        save_record(record)
        if reply.get("kind") != "timeout":
            raise RelayError(f"the app rejected the steer: {reply.get('error')}", kind=reply.get("kind"))
        raise RelayError("the steer was sent but has not appeared in the task. Do not resend: run `status`.",
                         kind="unconfirmed")
    record["last_operation"]["outcome"] = outcome
    save_record(record)
    result = {"task": task, "action": "steered", "outcome": outcome}
    if outcome == "accepted":
        result["detail"] = "the app accepted the steer; Codex will read it at its next step"
    if arguments.wait:
        result["wait"] = wait_loop(task, record, remaining(arguments, command_started), arguments.progress)
    return emit(result)


def command_interrupt(arguments):
    task = validate_task(arguments.task)
    record = require_control(task)
    state, tracker = inspect(task, record)
    if state["state"] not in ("working", "waiting_for_input", "waiting_for_approval"):
        return emit({"task": task, "action": "none", "state": state["state"],
                     "detail": "nothing is running, so nothing was interrupted"}, EXIT_WRONG_STATE)
    path = find_rollout(task, record)
    offset = os.path.getsize(path)
    connection = IpcConnection()
    try:
        owner = find_owner(connection, task, open_if_missing=True, background=True)
        reply = follower_request(connection, owner, "thread-follower-interrupt-turn",
                                 {"conversationId": task, "mode": "user-stop"}, timeout=15, version=3)
        deadline = time.time() + 10
        stopped = False
        while time.time() < deadline and not stopped:
            events, _ = read_events(path, offset)
            stopped = any((event.get("payload") or {}).get("type") in ("turn_aborted", "task_complete")
                          for event in events)
            time.sleep(0.5)
        snapshot = app_snapshot(connection, owner, task)
    finally:
        connection.close()
    running = summarize_snapshot(snapshot)["running_commands"] if snapshot else None
    record["last_operation"] = {"kind": "interrupt", "at": time.time(), "rollout_offset": offset,
                                "outcome": "stopped" if stopped else "unconfirmed"}
    save_record(record)
    result = {"task": task, "action": "interrupted" if stopped else "interrupt_sent",
              "model_turn_stopped": stopped, "commands_still_running": running,
              "detail": "Interrupt stops Codex's turn. Shell commands it already started keep running until they "
                        "exit; `commands_still_running` lists what the app still shows as running "
                        "(null means the app could not be asked)."}
    if not reply["ok"]:
        result["ipc_error"] = reply["error"]
    return emit(result, EXIT_OK if stopped else EXIT_FAILED)


def command_settings(arguments):
    task = validate_task(arguments.task)
    record = require_control(task)
    if not (arguments.model or arguments.effort):
        raise RelayError("give --model and/or --effort", kind="invalid_input", exit_code=EXIT_INVALID)
    state, tracker = inspect(task, record)
    if state["state"] in ("working", "waiting_for_input", "waiting_for_approval"):
        raise RelayError("settings apply to the next turn; wait until the current turn ends",
                         kind="wrong_state", exit_code=EXIT_WRONG_STATE)
    connection = IpcConnection()
    try:
        owner = find_owner(connection, task, open_if_missing=True, background=True)
        requested = apply_settings(connection, owner, task, record, tracker, arguments.model, arguments.effort)
        snapshot = app_snapshot(connection, owner, task)
    finally:
        connection.close()
    save_record(record)
    actual = summarize_snapshot(snapshot) if snapshot else {}
    confirmed = bool(snapshot) and all(actual.get(key) == value for key, value in requested.items() if value)
    return emit({"task": task, "action": "settings", "requested": requested,
                 "app_reports": {"model": actual.get("model"), "effort": actual.get("effort")},
                 "confirmed": confirmed,
                 "detail": "applies to the next turn; `send` verifies it again when that turn starts"},
                EXIT_OK if confirmed else EXIT_FAILED)


def wait_loop(task, record, timeout, progress):
    """Bounded wait. Returns on a final state, a need for the user, a lost task, a new
    agent message (with progress), or the timeout (still working). Only the session that
    controls the task advances its message cursor."""
    path = find_rollout(task, record)
    owned = record.get("session") == current_session()
    operation = (record.get("last_operation") or {}) if owned else {}
    end_of_file = os.path.getsize(path)
    base = operation.get("rollout_offset", end_of_file) if operation.get("kind") in ("start", "steer") else end_of_file
    cursor = record.get("wait_cursor") if owned and record.get("wait_cursor") is not None else base
    tracker = track_from(path, operation["rollout_offset"]) if operation.get("kind") == "start" else track_whole(path)
    deadline = time.time() + max(timeout, 1)
    app_check_interval, next_app_check = QUIET_SECONDS_BEFORE_APP_CHECK, 0
    new_messages, state = [], tracker.state()

    def finish(result):
        if owned:
            record["wait_cursor"] = cursor
            save_record(record)
        return result

    while True:
        events, cursor = read_events(path, cursor)
        new_messages += [found[1] for found in map(message_of, events) if found and found[0] == "agent"]
        tracker.feed(path)
        state = tracker.state()
        if tracker.turn_id is None and operation.get("kind") == "start":
            state = "starting"
            if time.time() - operation.get("at", time.time()) > 45:
                state = "not_started"
                break
        if state in ("completed", "interrupted", "failed", "lost", "waiting_for_input") or (progress and new_messages):
            break
        quiet = tracker.seconds_since_activity()
        if quiet is not None and quiet < QUIET_SECONDS_BEFORE_APP_CHECK:
            # Activity resets the backoff: the next silence is checked after 15 s again.
            app_check_interval, next_app_check = QUIET_SECONDS_BEFORE_APP_CHECK, 0
        elif state == "working" and time.time() >= next_app_check:
            # Approvals, lost turns and running commands are only visible in the app.
            # Check while Codex is silent, backing off to spare large snapshots.
            checked, _ = inspect(task, record)
            if checked["state"] != "working":
                return finish(dict(checked, messages=[message[-2000:] for message in new_messages[-3:]]))
            next_app_check = time.time() + app_check_interval
            app_check_interval = min(app_check_interval * 2, 60)
        if time.time() >= deadline:
            break
        time.sleep(1)
    result = {"task": task, "state": "working" if state == "starting" else state, "turn_id": tracker.turn_id,
              "messages": [message[-2000:] for message in new_messages[-3:]],
              "last_activity_seconds": tracker.seconds_since_activity(), "model": tracker.model,
              "effort": tracker.effort}
    if state == "waiting_for_input":
        result["questions"] = tracker.pending_questions()
    if state == "not_started":
        result["detail"] = "no turn appeared after the last start request; check `status` before resending"
    if state in ("completed", "failed"):
        result["final_message"] = tracker.final_message
        result["error"] = tracker.turn_error
    if state in ("working", "starting") and time.time() >= deadline:
        result["detail"] = "still working when the wait ended; call wait again"
    return finish(result)


def command_wait(arguments):
    task = validate_task(arguments.task)
    record = load_record(task) or {"task": task}
    find_rollout(task, record)
    result = wait_loop(task, record, arguments.timeout, arguments.progress)
    return emit(result)


def command_status(arguments):
    task = validate_task(arguments.task)
    record = load_record(task)
    state, tracker = inspect(task, record, use_app=not arguments.rollout_only)
    state["controlled_by_this_session"] = bool(record and record.get("session") == current_session())
    state["last_operation"] = (record or {}).get("last_operation")
    return emit(state)


def command_read(arguments):
    task = validate_task(arguments.task)
    events, _ = read_events(find_rollout(task, load_record(task)))
    messages = [found for found in map(message_of, events) if found]
    return emit({"task": task, "messages": [{"role": role, "text": text[-arguments.characters:]}
                                            for role, text in messages[-arguments.last:]]})


def command_list(arguments):
    records = {}
    for path in glob.glob(os.path.join(state_directory(), "tasks", "*.json")):
        try:
            record = json.load(open(path))
            records[record["task"]] = record
        except (OSError, ValueError, KeyError):
            continue
    rows = []
    if arguments.all:
        paths = glob.glob(os.path.join(codex_home(), "sessions", "*", "*", "*", "rollout-*.jsonl"))
        paths.sort(key=os.path.getmtime, reverse=True)
        tasks = []
        for path in paths:
            with open(path, "rb") as handle:
                first = handle.readline()
            try:
                meta = json.loads(first).get("payload") or {}
            except ValueError:
                meta = {}
            if "subagent" in json.dumps(meta.get("source", "")):
                continue
            tasks.append((path[-42:-6], path))
            if len(tasks) >= arguments.limit:
                break
    else:
        tasks = [(task, None) for task in sorted(records, key=lambda key: -(records[key].get("updated_at") or 0))
                 if records[task].get("session") == current_session()][:arguments.limit]
    for task, path in tasks:
        try:
            path = path or find_rollout(task, records.get(task))
            tracker = track_whole(path)
            first_user = ""
            for event in read_events(path)[0][:400]:
                found = message_of(event)
                if found and found[0] == "user" and found[1].strip():
                    first_user = found[1].strip().splitlines()[0][:80]
                    break
            state = tracker.state()
        except RelayError as error:
            state, first_user = error.kind, ""
        record = records.get(task) or {}
        rows.append({"task": task, "state": state, "title": first_user,
                     "control": "this session" if record.get("session") == current_session()
                     else ("other session" if record else "none"),
                     "updated": time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
                     if path and os.path.exists(path) else None})
    return emit({"tasks": rows, "note": "states come from rollouts; approvals need `status`"})


def focus_terminal():
    """Bring back the app that launched this agent (Terminal, iTerm, Ghostty...)."""
    bundle_identifier = os.environ.get("__CFBundleIdentifier")
    if bundle_identifier:
        subprocess.run(["open", "-b", bundle_identifier], check=False)
        return bundle_identifier
    names = {"Apple_Terminal": "Terminal", "iTerm.app": "iTerm", "ghostty": "Ghostty", "WarpTerminal": "Warp",
             "vscode": "Visual Studio Code", "WezTerm": "WezTerm"}
    name = names.get(os.environ.get("TERM_PROGRAM", ""))
    if name:
        subprocess.run(["open", "-a", name], check=False)
    return name


NOTIFICATIONS = {
    "start": "Claude is taking control of Codex in the ChatGPT app.",
    "completed": "Claude is done. The Codex task completed; the summary is in your terminal.",
    "cancelled": "Claude stopped the Codex task. Details are in your terminal.",
    "failed": "The Codex task failed. Details are in your terminal.",
    "needs-input": "Codex is waiting for your answer in the ChatGPT app. Claude has handed control back.",
    "needs-approval": "Codex is waiting for your approval in the ChatGPT app. Claude has handed control back.",
}


def command_notify(arguments):
    phase = "start" if arguments.phase == "start" else arguments.outcome
    if not phase:
        raise RelayError("`notify end` needs --outcome", kind="invalid_input", exit_code=EXIT_INVALID)
    message = NOTIFICATIONS[phase]
    script = f"display notification {json.dumps(message)} with title \"Codex relay\" sound name \"Glass\""
    subprocess.run(["osascript", "-e", script], check=False)
    focused = focus_terminal() if arguments.phase == "end" and not arguments.no_focus else None
    return emit({"notified": message, "focused": focused})


def command_doctor(arguments):
    checks = {"relay_version": RELAY_VERSION, "python": sys.version.split()[0], "platform": sys.platform,
              "codex_home": codex_home(), "state_directory": state_directory(), "session": current_session()}
    problems = []
    if sys.platform != "darwin":
        problems.append("only macOS is supported")
    if sys.version_info < (3, 8):
        problems.append("python 3.8 or newer is required")
    app = find_app()
    checks["app"] = app
    if app:
        checks["app_version"] = app_version(app)
        checks["app_version_tested"] = checks["app_version"] in TESTED_APP_VERSIONS
        try:
            checks["protocol"] = check_protocol_compatibility(force=True)
        except RelayError as error:
            checks["protocol"] = error.details
            problems.append(str(error))
    else:
        problems.append("ChatGPT desktop app not found")
    try:
        checks["codex_binary"] = codex_binary()
    except RelayError as error:
        problems.append(str(error))
    checks["socket"] = socket_path()
    if checks["socket"]:
        try:
            IpcConnection().close()
            checks["ipc"] = "handshake ok"
        except RelayError as error:
            problems.append(str(error))
    else:
        problems.append("the app is not running, or it uses a different CODEX_HOME")
    checks["problems"] = problems
    return emit(checks, EXIT_OK if not problems else EXIT_APP)


class JsonArgumentParser(argparse.ArgumentParser):
    """Usage errors come out as JSON with exit code 2, like every other failure."""

    def error(self, message):
        emit({"ok": False, "error": f"{self.prog}: {message}", "kind": "invalid_input"})
        sys.exit(EXIT_INVALID)


def main():
    parser = JsonArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    def prompt_options(subparser):
        subparser.add_argument("--prompt")
        subparser.add_argument("--prompt-file")
        subparser.add_argument("--image", action="append", help="local image to attach; repeatable")

    def wait_options(subparser):
        subparser.add_argument("--wait", action="store_true", help="wait after acting (bounded by --timeout)")
        subparser.add_argument("--timeout", type=float, default=100,
                               help="seconds for the whole command, setup included; keep below the calling "
                                    "tool's limit (default 100)")
        subparser.add_argument("--progress", action="store_true", help="also return at the next Codex message")

    sub = commands.add_parser("new", help="create a task in the app and start its first turn")
    prompt_options(sub); wait_options(sub)
    sub.add_argument("--cwd", default="~/Desktop")
    sub.add_argument("--title"); sub.add_argument("--model"); sub.add_argument("--effort")
    sub.add_argument("--background", action="store_true", help="do not bring the app to the front")
    sub.add_argument("--bootstrap-timeout", type=float, default=90)
    sub.set_defaults(handler=command_new)

    sub = commands.add_parser("send", help="start a new turn on an idle task")
    sub.add_argument("task"); prompt_options(sub); wait_options(sub)
    sub.add_argument("--model"); sub.add_argument("--effort")
    sub.add_argument("--background", action="store_true")
    sub.add_argument("--allow-repeat", action="store_true", help="resend a prompt already delivered")
    sub.set_defaults(handler=command_send)

    sub = commands.add_parser("steer", help="add input to the running turn")
    sub.add_argument("task"); prompt_options(sub); wait_options(sub)
    sub.add_argument("--background", action="store_true")
    sub.set_defaults(handler=command_steer)

    sub = commands.add_parser("interrupt", help="stop the running turn")
    sub.add_argument("task"); sub.set_defaults(handler=command_interrupt)

    sub = commands.add_parser("settings", help="set model/effort for the next turn")
    sub.add_argument("task"); sub.add_argument("--model"); sub.add_argument("--effort")
    sub.set_defaults(handler=command_settings)

    sub = commands.add_parser("wait", help="bounded wait for progress or an outcome")
    sub.add_argument("task")
    sub.add_argument("--timeout", type=float, default=100)
    sub.add_argument("--progress", action="store_true")
    sub.set_defaults(handler=command_wait)

    sub = commands.add_parser("status", help="current state of a task")
    sub.add_argument("task"); sub.add_argument("--rollout-only", action="store_true")
    sub.set_defaults(handler=command_status)

    sub = commands.add_parser("read", help="recent messages of a task")
    sub.add_argument("task"); sub.add_argument("--last", type=int, default=6)
    sub.add_argument("--characters", type=int, default=4000)
    sub.set_defaults(handler=command_read)

    sub = commands.add_parser("list", help="tasks this session controls (--all: recent tasks)")
    sub.add_argument("--all", action="store_true"); sub.add_argument("--limit", type=int, default=15)
    sub.set_defaults(handler=command_list)

    sub = commands.add_parser("adopt", help="take control of a task the user named")
    sub.add_argument("task"); sub.set_defaults(handler=command_adopt)

    sub = commands.add_parser("open", help="show a task in the app")
    sub.add_argument("task"); sub.add_argument("--background", action="store_true")
    sub.set_defaults(handler=command_open)

    sub = commands.add_parser("notify", help="macOS banner; end also refocuses the terminal")
    sub.add_argument("phase", choices=["start", "end"])
    sub.add_argument("--outcome", choices=["completed", "cancelled", "failed", "needs-input", "needs-approval"])
    sub.add_argument("--no-focus", action="store_true")
    sub.set_defaults(handler=command_notify)

    sub = commands.add_parser("doctor", help="check the app, protocol, paths and binary")
    sub.set_defaults(handler=command_doctor)

    arguments = parser.parse_args()
    try:
        sys.exit(arguments.handler(arguments) or EXIT_OK)
    except RelayError as error:
        sys.exit(emit(dict({"ok": False, "error": str(error), "kind": error.kind}, **error.details),
                      error.exit_code))


if __name__ == "__main__":
    main()
