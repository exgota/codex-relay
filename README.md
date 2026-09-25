# codex-relay

A Claude Code skill that hands work to Codex in the ChatGPT desktop app and supervises it while you watch. Claude writes the brief, starts a Codex task, waits, steers it when something is actually wrong, and checks the result. Every step shows up in the app as a normal Codex thread, so you can read along or take over at any point.

It suits jobs where Codex is the better tool: computer use, the app's browser with your saved logins, GPT Image generation, or anything you want a GPT model on. It needs macOS and the ChatGPT desktop app with Codex, signed in and running.

## What it does

- **Starts tasks you can watch.** Each task is a Codex thread in the app. Claude starts it at the model and effort you asked for, and confirms both from the turn's own record. If they don't match, the relay stops the turn.
- **Waits without babysitting.** Waits are bounded, so they fit Claude Code's tool time limit. Each wait resumes where the last one stopped and returns only new messages.
- **Knows what state Codex is in:** working, waiting for your answer, waiting for your approval, done, interrupted, failed, or cut off. Questions and approvals always come back to you. The relay never answers them.
- **Steers on purpose.** Starting a turn and changing a running turn are separate commands, so a new brief can't slip into work that's already running.
- **Stays in its lane.** It only controls tasks its own Claude session created, or tasks you name.
- **Taps you in and out.** You get a macOS banner when Claude takes over and another when it hands back, which also brings your terminal to the front.

## Requirements

- macOS. The relay uses a Unix socket, `open` and `osascript`. Windows and Linux aren't supported.
- The ChatGPT desktop app with Codex, signed in and running. Tested with versions 26.917.51856 and 26.917.71314. Before its first use on any other version, the relay checks that the app still speaks the protocol it expects.
- Claude Code.
- Python 3.8 or later, standard library only. Tested with 3.9.6, the macOS system Python, and with 3.14.7.
- The `codex` command is optional. The relay falls back to the copy inside the app.

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

Then check the setup from the skill's folder:

```bash
python3 scripts/codex_relay.py doctor
```

`"problems": []` means the app, its protocol, the `codex` binary, `CODEX_HOME` and the app's socket all check out. The first time Claude runs the script, Claude Code may ask for permission. Allow it for this skill.

## Use

Ask Claude in plain words: "have Codex check my three latest invoices in the browser and summarize them", "use Codex to make a hero image for this page", "how is the Codex task doing?", "stop the Codex task". Claude follows `SKILL.md`: brief, start, wait, step in only when needed, verify, hand back.

The script works on its own too:

```bash
python3 scripts/codex_relay.py new --title "Invoice summary" --prompt-file brief.md --effort medium --wait --timeout 540
python3 scripts/codex_relay.py wait <task> --timeout 540
python3 scripts/codex_relay.py steer <task> --prompt "Use the March invoice, not February."
python3 scripts/codex_relay.py --help
```

Each command prints one JSON object. `new` also prints the task id on a line of its own as soon as the task exists. A finished wait, trimmed:

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

`state` is one of `working`, `waiting_for_input` (with the questions Codex asked), `waiting_for_approval`, `completed`, `interrupted`, `failed`, `lost` (cut off, will never finish), `disconnected` (an open turn no app window is running), `not_started` or `idle`. Exit codes describe the command itself: 0 ok, 1 failed, 2 invalid input, 3 wrong state for that action, 4 app unavailable or incompatible, 5 a task this session doesn't control.

## Updating

Once a day the script checks this repository for a newer version. When one exists, its output includes `update_available`, and the skill tells Claude to ask you before updating. To update:

- Claude Code plugin: `/plugin marketplace update codex-relay`, then `/plugin update codex-relay@codex-relay`
- Skills CLI: `npx skills update codex-relay`
- By hand: copy the new `skills/codex-relay/` over the old one.

Set `CODEX_RELAY_NO_UPDATE_CHECK=1` to turn the check off.

## What leaves your machine

- **Your briefs go to Codex.** They reach OpenAI like any message you type into the app. The relay itself talks only to the app, over a socket on your Mac.
- **One request a day** to `raw.githubusercontent.com` for the update check, unless you turn it off.
- **Nothing else.** The relay keeps a small record per task in `~/.local/state/codex-relay`: ids, states and a hash of the last prompt. Prompt text isn't stored.

## How it works

The ChatGPT app runs its own private Codex server, so Codex's documented protocol can't reach the threads you see in the app. The app does coordinate its own windows over a local socket (`~/.codex/ipc/ipc.sock`): the window running a thread owns it, and other windows send it requests. The relay joins that socket the way a second window would, so the app treats everything it sends like input typed into it. For progress it reads the thread's log file, which the app writes as Codex works. For what the log can't show, such as pending approvals and commands still running, it asks the app for a live snapshot.

The protocol is undocumented. [`references/ipc-protocol.md`](skills/codex-relay/references/ipc-protocol.md) records it, along with how to re-derive it after an app update.

## Testing

Tested on macOS with ChatGPT app 26.917.71314, on scratch tasks only. The live checks covered:

- a task from creation to a checked result
- effort confirmed on new and existing tasks
- a follow-up turn that used the earlier answer
- a mid-turn steer
- a question and an approval, both handed back
- cancelling while a command was running
- a forced timeout that recovered without sending twice
- two tasks at once, where the untouched one stayed byte-identical
- refusals for blank input, bad ids and other sessions' tasks
- a fresh install on the system Python, without `codex` on PATH, and with a custom `CODEX_HOME`
- an incompatible app refused before anything ran
- the banner and terminal refocus

Three independent reviews reproduced these results, and every defect they found was fixed and re-checked.

Not tested:

- the app quitting in the middle of a turn
- Intel Macs
- app versions other than the two above
- whether the banner actually appeared (the terminal refocus is confirmed)

## Limitations

- **An extra first message on new tasks.** The app can't open a thread with nothing in it yet, so `new` first runs one short setup turn outside the app. Codex replies "ready". That turn carries only the task's title, never your brief.
- **Interrupting doesn't stop running commands.** It stops Codex's turn, but shell commands Codex already started run until they exit. The relay lists them.
- **Some slowdowns are ambiguous.** The relay can't tell a slow model from a stalled service. It reports how long Codex has been silent.
- **Approvals show up with a delay.** Only the app sees them, so the relay checks while Codex is quiet: about 15 seconds into a silence, then at widening intervals of up to a minute.
- **Archiving happens in the app.** `codex archive` can't touch a thread the app has open.
- **No remote hosts.** SSH and remote-control machines in the app aren't supported.
- **App updates can break it.** When an update changes the protocol, the relay stops with `incompatible_app` instead of guessing.

## Disclaimer

This is an unofficial project, not affiliated with or endorsed by OpenAI or Anthropic. ChatGPT and Codex are trademarks of OpenAI, and Claude is a trademark of Anthropic. The relay depends on an undocumented part of the ChatGPT desktop app that OpenAI can change at any time.

Codex runs with your app's own sandbox and approval settings. Any process on your Mac running as you can use the app's socket, so review what you hand off, and keep approvals turned on for anything you care about.

## License

MIT. See [LICENSE](LICENSE).
