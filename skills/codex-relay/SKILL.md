---
name: codex-relay
description: Delegate work to Codex in the ChatGPT desktop app and supervise it while the user watches. Start tasks, check progress, steer, stop. Use when the user asks to hand work to Codex or a GPT model, to use Codex's logged-in browser or computer use, to generate images with GPT Image, or to check on, steer or stop a Codex task they named.
license: MIT
compatibility: Requires macOS, the ChatGPT desktop app with Codex (signed in and running), and Python 3.8 or later. Tested with ChatGPT app 26.917.51856 and 26.917.71314.
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/codex_relay.py:*)
metadata:
  version: "1.0.0"
---

# Codex relay

Codex does the work inside the ChatGPT desktop app, where the user can watch and take over. Claude writes the brief, supervises at checkpoints, and verifies the result. A **task** is one Codex thread.

`RELAY` below means `python3 ${CLAUDE_SKILL_DIR}/scripts/codex_relay.py` (in agents other than Claude Code: `scripts/codex_relay.py` inside this skill's folder). Every command prints one JSON object on stdout, except `new`, which first prints a line with the task id. The exit code describes the command: 0 ok, 1 failed, 2 invalid input, 3 wrong state for that action, 4 app unavailable or incompatible, 5 task not controlled by this session. The task's own condition is in the `state` field, so a `wait` that reports `failed` still exits 0. For the long commands (`new`, `send`, `steer` with `--wait`, and `wait`), set the Bash tool's timeout to 600000 and pass `--timeout 540`. The wait gets whatever remains of `--timeout` after setup (minimum 5 s), so the whole command stays within the Bash limit.

Commands: `new`, `send`, `steer`, `wait`, `interrupt`, `settings`, `status`, `read`, `list`, `adopt`, `open`, `notify`, `doctor`. `RELAY <command> --help` shows the flags.

## Run order

1. **Announce.** Tell the user in one line that Claude is taking control of Codex, and run `RELAY notify start`.
2. **Brief.** Write the brief to a file with the template in `references/briefing.md`.
3. **Start or continue.**
   - For new work: `RELAY new --title "<short label>" --prompt-file <brief> --effort <e> [--model <m>] --wait --timeout 540`. The title is only a sidebar label for the app's one-word setup turn, and the brief never goes into that turn. The first output line carries the task id as soon as the task exists, so keep it. New tasks work in `~/Desktop` by default (`--cwd` changes that).
   - For a follow-up on the same work, reuse the task: `RELAY send <task> --prompt-file <brief> --wait --timeout 540`. Start a new task only for unrelated work.
   - Pass `--effort` and `--model` whenever they matter. The result reports the model and effort the turn is actually running with, read from the turn's own record. If they differ from the request, the relay stops the turn and fails. `settings_verified: false` means the turn started before recording them. Check `status` shortly after and compare.
4. **Supervise.** Act on the returned `state` (table below). While the state is `working`, run `RELAY wait <task> --timeout 540` again. Each wait resumes where the last one stopped and returns only new Codex messages.
5. **Intervene** only for a concrete misunderstanding or missing information (`steer`), a permission boundary (hand back to the user), or repeated failure (`interrupt`, then a corrected brief). If the evidence you need is already in hand, skip extra checks and extra review rounds.
6. **Verify and hand back.** Check the result against the brief's `Done when`. When Codex reports "saved" or "done", open the thing itself, or have Codex reload it and read the value back. Then run `RELAY notify end --outcome <completed|cancelled|failed|needs-input|needs-approval>`, which posts a banner and brings the terminal back. Report what Codex did, what you verified, and what remains unverified.

If any result carries `update_available`, a newer version of this skill exists. Tell the user once, and update only if they agree, following the field's instructions.

The relay is done when the user's outcome has been observed, or control has been handed back for their input, and `notify end` has run with the matching outcome.

## States

| state | meaning | next move |
|---|---|---|
| `working` | a turn is running | `wait` again. `last_activity_seconds` shows how long Codex has been silent. |
| `waiting_for_input` | Codex asked the user a question | hand back: `notify end --outcome needs-input`, and give the user the `questions` field verbatim |
| `waiting_for_approval` | Codex needs an approval | hand back: `notify end --outcome needs-approval`. Approvals always stay with the user. |
| `completed` | the turn finished | verify (step 6), or `send` the next related turn |
| `interrupted` | the turn was stopped | decide whether to `send` a corrected brief |
| `failed` | the turn ended with an error (`error` says why) | report it; retry once only if the error is transient |
| `lost` | the last turn was cut off and will never finish | `send` starts a new turn |
| `disconnected` | the rollout shows an open turn, but the app isn't running it | `send` reopens the task in the app and continues; if that fails, tell the user |
| `not_started` | a start request left no trace | run `status`, and resend only if it's still not started |
| `idle` | the task has no turns yet | `send` the first brief |

Detection limits: approval requests and running commands are visible only while the app has the task loaded. They're checked while Codex is silent, about 15 s into a silence and then at widening intervals of up to 60 s. `status` reports this under `limits`. A missing or uncertain state never means Codex is allowed to proceed. The relay can't tell a slow model from a stalled service. After several minutes of silence in `working`, tell the user instead of retrying on your own.

## Control rules

- Control only tasks this Claude session created, or tasks the user explicitly named. For a named task, run `RELAY adopt <task>` once. The script refuses every other task with exit code 5. Task ids must be the full 36-character UUID.
- `send` starts a turn only on an idle task and refuses while one is running. `steer` changes the running turn and refuses otherwise. A steer outcome of `accepted` means the app took it and Codex will read it at its next step, which a running command can delay. After an `unconfirmed` outcome or any timeout, run `status` before doing anything else, and never resend blindly. `send` also refuses to resend a prompt that was already delivered.
- `interrupt` stops Codex's turn. Shell commands Codex already started keep running until they exit, and `commands_still_running` lists them.
- Answering questions and approvals is the user's job. Relay the user's own answer with `send` only after the turn has ended.
- Run one Codex task at a time by default. It's lighter on the machine and gives the user one thing to watch. Run more in parallel only when the user asks.

## Observing versus pausing

`--progress` (on `wait`, or on `new`/`send`/`steer` with `--wait`) returns at the next Codex message. That message is progress, not necessarily a plan, and Codex keeps working. To actually pause for review, write it into the brief ("stop after the plan and report"). The turn then completes, you review it, and you `send` the go-ahead.

## Recovery

- `RELAY doctor` checks the app, the protocol version, `CODEX_HOME`, the `codex` binary and the socket.
- A `new` that ends in `bootstrap_timeout`: if `written` is true, continue with `send <task>` (it recovers the task). If `written` is false, nothing exists, and running `new` again is safe.
- A `kind` of `incompatible_app` means the app update changed the protocol. Stop, tell the user, and point to `references/ipc-protocol.md`, section "After an app update".
- The relay never falls back to headless `codex exec` on its own, because that would lose the app's browser, plugins and visibility. Use headless runs only when the user asks for unwatched batch work that needs none of those.

## Images

For image generation, read `references/image-prompting.md`. If `references/local/image-examples.md` exists, read it as well. It holds this user's own image setup and preferences.
