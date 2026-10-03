# Roadmap

Leads found while updating the relay for ChatGPT app 26.930.21537 (2026-10-03), in priority order. None of them is built yet. Each entry gives what is known, the evidence, how to test it, and what can go wrong. Read `skills/codex-relay/references/ipc-protocol.md` first.

Test rules for every item: use scratch tasks in a scratch folder only. Never send to, steer, interrupt or open a thread the user is working in without asking. Reading a thread through the relay is fine, but `status` on an open turn asks the app for a full snapshot, which is heavy on large threads.

## 1. Dots (always-on agents)

OpenAI launched dots on 2026-09-29: always-on agents on GPT-6 Astra, each with its own cloud computer and browser, reachable from ChatGPT, Codex, Slack and Teams, with no public API. The app's code calls a dot an "aeon" (also "orbit" and "tbo"). The author's primary dot is named frog.

What is known:

- A dot's conversation is a Codex thread on a remote host the app calls `durable`, with `threadSource` `aeon`. It is not in `state_5.sqlite` or `~/.codex/sessions`, so `list --all` and the rollout reads can't see it.
- The primary dot's thread id is in `~/.codex/.codex-global-state.json`, at `electron-persisted-atom-state` → `primary-aeon-selection-v1` → `response.selection.thread_id`. The same object holds `aeon_id` and `messaging_room_id`. `cloud-aeon-sidebar-cache-v1` maps thread ids to dot profiles.
- `thread-owner-discovery` for the dot thread returned `no-client-found` with `hostId` `durable` and `local`, in the params and on the request. No window had the thread loaded at the time.
- The app's dot code uses backend routes `/tbo/primary`, `/tbo/{tbo_id}/messaging-room`, `/messaging/rooms/{room_id}/messages`, `/tbo/{tbo_id}/channels/email` and `/aeon-messaging/phone`. A dot has its own email address ("Your dot's email address" in the app). Texting is "coming soon".
- A dot can start Codex tasks (cloud, or local in Work or Codex), which count against Codex limits. Those local tasks are visible to the relay like any other.

Next steps:

1. With the user's go-ahead, since this switches their visible Codex window, load the dot thread: `open -g "codex://threads/<thread id>?hostId=durable"`. Then retry owner discovery with `hostId: "durable"`. Try both the params field and the request-level `hostId`. Remote-host `thread-follower-*` versions are one higher than local.
2. If an owner answers, take one snapshot: follow broadcast with `hostId: "durable"`, then `thread-follower-load-complete-history`. Record which `summarize_snapshot` fields still apply.
3. Only then, and only when the user's own work doesn't depend on the dot, send one harmless message. Check whether `thread-follower-start-turn` works on a dot thread or whether the app sends dot messages through the messaging room instead. Look for `messageThreadId` and `mode: "durable"` in `bootstrap-*.js` near the `durable` checks.
4. If the relay can't reach dots, document email (for example through a Gmail connector) as the fallback channel. Also document asking the dot to open a local Codex task, which the relay then watches.

Risks: dot usage limits after the first month are unpublished. Dots act on connected apps and the user's computer, so anything sent to one needs the same authority rules as a Codex brief.

## 2. Queued follow-ups

26.930.21537 added `thread-follower-remove-queued-message` and `thread-follower-clear-queued-messages`, next to the older `thread-follower-set-queued-follow-ups-state`. The broadcast is `thread-queued-followups-changed` (v2), and the app's default queue mode is in `electron-initial-follow-up-queue-mode`. The CLI has `codex queue --thread <id> --message <text>`, but that talks to the shared app-server daemon, which the desktop app doesn't use.

The goal is a `send --queue` that lines up the next brief while a turn runs, instead of steering or waiting. Next step: read `turnCoordinator.acceptFromFollower` and the queue state shape in `app-shared-*.js`, then test on a scratch task.

## 3. Resuming or clearing a paused goal

An `interrupt` pauses a goal. The model can't replace a paused goal (`create_goal` fails) or close an unfinished one (`update_goal` allows `complete` only once the objective is met). The app resumes and clears goals with app-server `thread/goal/set` and `thread/goal/clear`, called only from the owning window, and no follower method reaches them.

Untested routes:

- Sending `/goal <objective>` as turn text through `thread-follower-start-turn`. The composer handles `/goal` before it starts a turn, so this probably arrives as plain text, but it has not been tried.
- Whether `thread-follower-update-thread-settings` or another follower method carries goal changes.

## 4. Letting Codex ask questions

`request_user_input` is unavailable in the default collaboration mode, so Codex rejects its own question at once and puts the question in its final message. The relay now waits 3 s before reporting `waiting_for_input`. Find out which collaboration mode allows the tool (likely plan mode, see `latestCollaborationMode` and `collaborationMode` in the bundle), and whether `threadSettings` can set it per task. Then the `waiting_for_input` hand-back works again.

## 5. Approvals on 26.930.21537

Approval detection was not re-tested on this build. Re-run the approval check from `ipc-protocol.md` ("After an app update", step 4) on a scratch task with on-request approvals. Restore the task's settings afterwards.

## 6. Warn when rollouts stop

Every thread now uses `history_mode = paginated`, stored in `thread_history_1.sqlite`. Rollouts are still written next to it, and every relay state depends on them. If a build stops writing them, the relay goes blind, and `doctor` wouldn't notice. Add a `doctor` check: for the most recently updated thread in `state_5.sqlite`, its rollout file should exist and have a recent modification time. Longer term, read turns from `thread_history_1.sqlite` (tables `thread_turns` and `thread_items`) when the rollout is missing.

## 7. Unread state for the cross-task view

`list --all` could flag threads with an unread turn. The app keeps them in `.codex-global-state.json` under `electron-thread-read-state-v1` and broadcasts `thread-read-state-changed` (v3, `{hostId, conversationId, hasUnreadTurn}`).

## 8. Watch for the shared app-server daemon

CLI 0.159 can run a shared app-server daemon (`codex app-server daemon`, socket `~/.codex/app-server-control/app-server-control.sock`). If a future desktop app moves its threads onto that daemon, the documented app-server protocol (`codex app-server generate-json-schema`) could replace the reverse-engineered window channel, goals included. Check at each app update whether the app still starts its own stdio `app-server`.

## Not planned

Changing a running turn's permissions (`update-thread-settings` with an `activeTurnId`) works since 26.930.21537, but approvals and permissions stay with the user, so the relay won't use it.
