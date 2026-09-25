# Codex desktop app: relay protocol reference

Undocumented and reverse-engineered. Verified on ChatGPT.app 26.917.51856 (2026-09-23) and 26.917.71314 (2026-09-24), macOS, bundled CLI 0.155.0-alpha.16.x. Before talking to a new app build, the relay compares the app's method-version table with its own and refuses to run on a mismatch (see "After an app update").

## Why this channel

The desktop app runs its own `codex app-server` as a private stdio child, so the documented app-server protocol can't reach the threads the app shows. The app coordinates its own windows over a local IPC router instead. The window that streams a thread from the app-server **owns** it. Other clients act as **followers**, and the owner carries out their requests through the app's own code path, so the result looks exactly like input typed into the app.

## Transport

- Socket: `$CODEX_HOME/ipc/ipc.sock` (default `~/.codex/ipc/ipc.sock`), mode 0600, with the owning uid checked. The fallback path is `$TMPDIR/codex-ipc/ipc-<uid>.sock`.
- Frame: a 4-byte little-endian length, then UTF-8 JSON. The maximum frame is 256 MiB.
- The Electron main process hosts the router. Each app window registers as a client of type `desktop`.

## Messages

| type | fields |
|---|---|
| `request` | `requestId`, `sourceClientId`, `version`, `method`, `params`, optional `targetClientId`, `hostId`, `timeoutMs` |
| `response` | `requestId`, `resultType` (`success`/`error`), `result` or `error`; success also carries `handledByClientId`, `method` |
| `broadcast` | `method`, `sourceClientId`, `version`, `params`, optional `targetClientIds` |
| `client-discovery-request` / `-response` | router ↔ client: `requestId`, `request` / `response: {canHandle}` |

- **Handshake:** a `request` with `method: initialize`, `version: 0`, `sourceClientId: "initializing-client"` and `params: {clientType}`. The reply is `result.clientId`, which becomes `sourceClientId` from then on.
- **Routing:** the router probes every other client (or only `targetClientId`) with `client-discovery-request`, and forwards the request to the first client that answers `canHandle: true`. Each probe has its own 10 s timer. An unanswered probe delays only the failure case, which is why the relay answers every probe with `canHandle: false`.
- **Timeouts:**
  - The router forwards with `timeoutMs`, or 10 s if none is given.
  - The owner wraps each follower method in its own timer. It reports `<method>-timeout`, observed once at 15 s on a start-turn that did in fact start.
- **Errors:**
  - A version mismatch returns `request-version-mismatch`. During discovery it shows up as `canHandle: false`, and so as `no-client-found`.
  - `no-client-found` is also the prefix of several owner-unreachable errors, such as `no-client-found: thread stream owner became unavailable`.

## Method versions (bundle table `nb`)

| method | version |
|---|---|
| `thread-owner-discovery` | 1 |
| `thread-follower-start-turn` | 2 |
| `thread-follower-steer-turn` | 1 |
| `thread-follower-interrupt-turn` | 4 with `expectedTurnId`, 3 without (local host) |
| `thread-follower-update-thread-settings` | 2 |
| `thread-follower-load-complete-history` | 1 |
| `thread-follower-update-daybreak` | 1 (new in 26.917.71314; unused) |
| `thread-follower-compact-thread`, `-submit-user-input`, `-*-approval-*`, `-set-queued-follow-ups-state` | 1 (unused) |
| `thread-follower-edit-last-user-turn` | 2 (unused) |
| `thread-stream-following-changed` (broadcast) | 1 |
| `thread-stream-state-changed` (broadcast) | 11 |

With a request-level `hostId` (remote hosts, not used by the relay), every `thread-follower-*` version is one higher. Interrupt is then always 5.

## Owner discovery

`thread-owner-discovery` with `params: {hostId: "local", conversationId}` returns the owner as `handledByClientId`, with `result.supportsUntrustedAppInput: true`. A thread has an owner only while a window has it loaded. `open "codex://threads/<id>"` loads it, and `open -g` does so without bringing the app forward.

## Methods the relay uses

All of these are sent to the owner with `targetClientId`.

- **Start a turn:** `thread-follower-start-turn` with `{conversationId, turnStart: {request: {threadId, input: [{type: "text", text, text_elements: []}, {type: "localImage", path}...]}, context: {}}}`. The owner fills in cwd, sandbox, approvals, model and effort from the thread's settings. **`model`/`effort` fields inside `request` did not take effect** in testing. Set them with the next method instead.
- **Set model and effort for the next turn:** `thread-follower-update-thread-settings` with `{conversationId, threadSettings: {model, effort}, activeTurnId: null, condition: null}`. This is exactly what the app's own composer sends, and it returns `{applied: true}`. The owner calls app-server `thread/settings/update`, falling back to local state when that method is missing. `threadSettings` also accepts `approvalPolicy` and `sandboxPolicy` (the tests used them on a scratch task). The relay never changes those.
- **Steer:** `thread-follower-steer-turn` with `{conversationId, input, restoreMessage: {text, cwd, context: {workspaceRoots: [cwd], commentAttachments: []}, responsesapiClientMetadata: {}}, serviceTier: null, attachments: [], clientUserMessageId, additionalContext: null, toolOutput: null}`. The owner finds the active turn itself, and fails with `no active turn to steer` when there isn't one.
- **Interrupt:** `thread-follower-interrupt-turn` (version 3) with `{conversationId, mode: "user-stop"}`, which returns `{interruptedTurnId, ok}`. Other modes are `system` (the default) and `descendant-cleanup`. It stops the model's turn. Shell processes the turn already started keep running.
- **Live snapshot:** broadcast `thread-stream-following-changed {conversationId, hostId: "local", following: true}`, then request `thread-follower-load-complete-history {conversationId}`. The owner broadcasts `thread-stream-state-changed` with `change.type: "snapshot"` and the full `conversationState`, and patches follow as the thread moves. Broadcast `following: false` afterwards. Without the follow step, the request fails with `no-client-found: thread stream owner became unavailable`. Fields the relay reads:
  - `threadRuntimeStatus`: `{type: idle | active | systemError | notLoaded, activeFlags: [waitingOnApproval | waitingOnUserInput]}`
  - `requests`: pending server requests
  - `latestThreadSettings`: `model`, `effort`, `approvalPolicy`, `sandboxPolicy`
  - `turnHistory.history`: turns in `islands` order, each with a `status` of `inProgress | completed | interrupted | failed`, and `items`, where a running command is an item with `status: inProgress`

## Rollout facts the relay depends on

The app writes `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread id>.jsonl` as the thread runs.

- The only `event_msg` types seen in a month of rollouts are `task_started`, `task_complete`, `turn_aborted`, `item_completed`, `thread_settings_applied` and `token_count`.
- `turn_context` (a top-level type) carries the turn's actual `model` and `effort`. It is written just after `task_started`.
- A failure is a `task_complete` with an `error` object (for example `serverOverloaded`). An interrupt is `turn_aborted` with `reason: interrupted`. A turn cut off by an app restart or a killed setup has **no** end event: 25 of 824 turns in September.
- A question to the user is a `response_item` `function_call` named `request_user_input` with no matching `function_call_output` yet.
- **Approval requests are never written to the rollout.** They're visible only in the live snapshot.
- Shell commands run asynchronously through the `exec` tool. A command shows up in the rollout (`item_completed` `CommandExecution`) only when it finishes, so a still-running command is visible only in the snapshot.

## Creating a task

No follower method creates a thread. App-server `thread/start` creates one (and reports a rollout path), but the rollout isn't written until the first turn, and the app can't open a thread with no rollout: owner discovery keeps returning `no-client-found`. So `new` bootstraps with `codex exec` and one minimal turn, which also records the requested model and effort. That turn is visible in the app as "ready". Threads made with a bare `thread/start` also came out read-only with on-request approvals, ignoring the user's config.

## Security note

Any process running as the user can drive Codex through this socket, with whatever sandbox and approval settings the threads use. The relay limits itself to tasks it created or was told to adopt, but the socket itself has no such limit.

## After an app update

1. Run `python3 scripts/codex_relay.py doctor`. It reads the app version, compares the bundle's method table with `METHOD_VERSIONS`, checks the `codex` binary and `CODEX_HOME`, and does an IPC handshake.
2. If the protocol check fails, re-derive it. `app.asar` is a Pickle header (4 × uint32 little-endian) followed by a JSON file table, and file bytes live at `8 + header_size + offset`. Extract the `.js` files containing `thread-follower`:
   - `.vite/build/src-*.js`: the router, the frame reader and the `nb` version table
   - `.vite/build/main-*.js` and `webview/assets/app-initial-*.js`: the owner's `handleThreadFollowerRequest` switch (it takes `case` + a backtick-quoted method name) and the follower-side request builders, which give the exact params
3. Update `METHOD_VERSIONS`, the param shapes and `TESTED_APP_VERSIONS` in the script.
4. Re-test on scratch tasks only, in this order: `doctor`, `new --effort low`, confirm the effort in the rollout's `turn_context`, `send` a follow-up at another effort, a steer during `sleep 20`, a question (`request_user_input`), an interrupt during `sleep 45`, and `notify end`.
