# codex-relay

A Claude Code skill that hands work to Codex in the ChatGPT desktop app and supervises it while you watch. Claude writes the brief, starts a Codex task, waits, steers it when something is wrong, and checks the result. Every step shows up in the app as a normal Codex thread, so you can read along or take over at any point.

It suits jobs where Codex is the better tool: computer use, the app's browser with your saved logins, GPT Image generation, or any job you want a GPT model to do. It needs macOS and the ChatGPT desktop app with Codex, signed in and running.

## What it does

- **Starts tasks you can watch.** Each task is a Codex thread in the app. Claude starts it at the model and effort you asked for, then reads both back from the turn's own record. If they don't match, the relay stops the turn. If the record isn't written within 90 seconds, it reports `settings_verified: false` instead.
- **Uses bounded waits.** Each wait ends before Claude Code's tool time limit, picks up where the last one stopped, and returns only messages it hasn't returned before.
- **Reports Codex's state:** working, waiting for your answer, waiting for your approval, done, interrupted, failed, or cut off. Questions and approvals always come back to you. The relay never answers them.
- **Keeps starting and steering separate.** One command starts a turn and another changes a running turn, so a new brief can't slip into work that's already running.
- **Controls only its own tasks.** It sends to, steers and stops only tasks this Claude session created or adopted. The skill tells Claude to adopt a task only when you name it. Reading any task's state is allowed.
- **Tells you when control changes.** Claude posts a macOS notification when it takes over and another when it hands back. The second one also brings your terminal to the front.

## Requirements

- macOS. The relay uses a Unix socket, `open` and `osascript`. Windows and Linux aren't supported.
- The ChatGPT desktop app with Codex, signed in and running. The first time any relay command talks to a new app version, the relay compares the app's protocol table with its own and refuses to run on a mismatch.
- Claude Code.
- Python 3.8 or later, standard library only.
- Node.js, only for the skills CLI install below.
- The `codex` command is optional. The relay falls back to the copy inside the app, which uses the app's own sign-in.

## Install

With the [skills CLI](https://github.com/vercel-labs/skills):

```bash
npx skills add exgota/codex-relay -g
```

As a Claude Code plugin:

```bash
/plugin marketplace add exgota/codex-relay
/plugin install codex-relay@codex-relay
```

By hand: copy `skills/codex-relay/` into `~/.claude/skills/`.

Then ask Claude to run the codex-relay doctor, or run it yourself from the skill's folder. That's `~/.claude/skills/codex-relay` for the skills CLI and by-hand installs. The plugin installs under `~/.claude/plugins`.

```bash
python3 scripts/codex_relay.py doctor
```

`"problems": []` means the app, its protocol, the `codex` binary, `CODEX_HOME` and the app's socket all check out. `"app_version_tested": false` is a warning, not a failure: the protocol matched, but nobody has tested that app version yet. The first time Claude runs the script, Claude Code may ask for permission. Allow it for this skill.

## Use

Ask Claude in plain words: "have Codex check my three latest invoices in the browser and summarize them", "use Codex to make a hero image for this page", "how is the Codex task doing?", "stop the Codex task". Claude follows `SKILL.md`: brief, start, wait, step in only when needed, verify, hand back. New tasks run in `~/Desktop` unless Claude passes `--cwd`.

On the first run you'll see the app come to the front, a new thread named after the task's title, and Codex answering "ready" before the real work starts. That's the setup turn described under Limitations.

The script also works on its own:

```bash
python3 scripts/codex_relay.py new --title "Invoice summary" --prompt-file brief.md --effort medium --wait --timeout 540
python3 scripts/codex_relay.py wait <task> --timeout 540
python3 scripts/codex_relay.py steer <task> --prompt "Use the March invoice, not February."
python3 scripts/codex_relay.py --help
```

Each command prints one JSON object. `new` first prints a one-line JSON object holding the task id as soon as the task exists, then the main result. A finished wait, trimmed:

```json
{
  "task": "0199f0c2-3b7e-7c61-9d2a-5a8f6b1e4c27",
  "state": "completed",
  "messages": ["Summary written to ~/Desktop/invoices.md"],
  "model": "gpt-6-astra",
  "effort": "medium",
  "final_message": "Summary written to ~/Desktop/invoices.md"
}
```

`state` is one of:

- `working`
- `waiting_for_input`, with the questions Codex asked
- `waiting_for_approval`
- `completed`
- `interrupted`
- `failed`
- `lost`: the turn was cut off and will never finish
- `disconnected`: the log shows an open turn, but no app window is running it
- `not_started`
- `idle`: the task has no turns yet

Exit codes describe the command itself: 0 ok, 1 failed, 2 invalid input, 3 wrong state for that action, 4 app unavailable or incompatible, 5 a task this session doesn't control.

## Updating

Once a day the script checks this repository for a newer version. When one exists, its output includes `update_available`, and the skill tells Claude to ask you before updating. To update:

- Claude Code plugin: `/plugin marketplace update codex-relay`, then `/plugin update codex-relay@codex-relay`
- Skills CLI: `npx skills update codex-relay`
- By hand: copy the new `skills/codex-relay/` over the old one.

Set `CODEX_RELAY_NO_UPDATE_CHECK=1` to turn the check off.

## What leaves your machine

- **Your briefs go to Codex.** They reach OpenAI like any message you type into the app. The relay talks to the app over a socket on your Mac.
- **The task's title goes to OpenAI.** To create a task, the relay runs the `codex` command once, which sends a short setup message containing only the title.
- **One request a day** to `raw.githubusercontent.com` for the update check, unless you turn it off.
- **Nothing else.** The relay keeps a small record per task in `~/.local/state/codex-relay`: ids, states and a hash of the last prompt. It stores no prompt text. Codex's own thread logs under `~/.codex/sessions` hold the full conversation, as they do for any thread, and a brief file Claude writes stays where Claude put it.

## How it works

The ChatGPT app runs its own private Codex server, so Codex's documented protocol can't reach the threads you see in the app. The app does coordinate its own windows over a local socket (`~/.codex/ipc/ipc.sock`): the window running a thread owns it, and other windows send it requests. The relay joins that socket the way a second window would, so the app treats everything it sends like input typed into it. For progress it reads the thread's log file, which the app writes as Codex works. For what the log can't show, such as pending approvals and commands still running, it asks the app for a live snapshot.

The protocol is undocumented. [`references/ipc-protocol.md`](skills/codex-relay/references/ipc-protocol.md) records it, along with how to re-derive it after an app update.

## Testing

Tested on macOS, on scratch tasks only. The protocol check passed on ChatGPT app versions 26.917.51856 and 26.917.71314, and the live checks below ran on 26.917.71314:

- a task from creation to a checked result
- effort confirmed on new and existing tasks
- a follow-up turn that used the earlier answer
- a mid-turn steer
- a question and an approval, both handed back
- cancelling while a command was running
- a forced timeout that recovered without sending twice
- two tasks at once, where the untouched one stayed byte-identical
- refusals for blank input, bad ids and other sessions' tasks
- a fresh install on the macOS system Python (3.9), without `codex` on PATH, and with a custom `CODEX_HOME`
- an incompatible app refused before anything ran
- the hand-back notification, delivered to macOS Notification Center, and the terminal refocus

Defects found in review were fixed and re-checked.

Not tested:

- the app quitting in the middle of a turn
- Intel Macs
- app versions other than the two above
- notifications on Macs where Script Editor isn't allowed to post them (`osascript` notifications appear under Script Editor)

## Limitations

- **An extra first message on new tasks.** The app can't open a thread with nothing in it yet, so `new` first runs one short setup turn outside the app. Codex is asked to reply with the single word "ready". That turn carries only the task's title, never your brief.
- **Interrupting doesn't stop running commands.** It stops Codex's turn, but shell commands Codex already started run until they exit. The relay lists them.
- **Some slowdowns are ambiguous.** The relay can't tell a slow model from a stalled service. It reports how long Codex has been silent.
- **Approvals show up with a delay.** Only the app sees them, so the relay checks while Codex is quiet: about 15 seconds into a silence, then at widening intervals of up to a minute.
- **Archive in the app.** The relay has no archive command. It reports an archived task as `archived` and won't send to it.
- **No remote hosts.** SSH and remote-control machines in the app aren't supported.
- **App updates can break it.** When an update changes the protocol, commands that talk to the app stop with `incompatible_app` instead of guessing. `doctor` shows what changed.

## Disclaimer

This is an unofficial project, not affiliated with or endorsed by OpenAI or Anthropic. ChatGPT and Codex are trademarks of OpenAI, and Claude is a trademark of Anthropic. The relay depends on an undocumented part of the ChatGPT desktop app that OpenAI can change at any time.

The relay never changes a task's sandbox or approval settings. Codex runs with whatever your app is set to, and in testing, approvals reached the user in the app as usual. The brief decides what Codex does, including in the app's browser with your saved logins, and approvals are the brake: keep them on for anything you care about. New tasks work in `~/Desktop` by default. Any process on your Mac running as you can use the app's socket, so review what you hand off.

## License

MIT. See [LICENSE](LICENSE).
