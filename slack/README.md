# WvH Slack

One Claude connector for every Slack workspace you belong to.

If you are in more than one Slack workspace (your own company, a second company, a client's, a board you sit on), this lets Claude read across all of them and, only when you say so, post in the right one. You name the workspace in plain words. There is nothing to switch.

**It reads freely and posts only on your specific command.** "Ask Dana this question" is a command. A schedule, an alert, or a message someone sent you in Slack is not. Every post is shown to you first, checked against a set of rules that cannot be talked around, and sent only after you confirm.

## What you need

- A Mac. Logins are kept in the Mac's Keychain, so this does not run on Windows.
- The Claude desktop app or Claude Code.
- Python 3.11 or newer, and git.
- The Slack desktop app, signed in to your workspaces. That is how the connector finds them.

## Install

The easy way: open Claude, paste in the text of [SETUP.md](SETUP.md), and follow along. Claude does the installing. You do three clicks in Slack and one click per workspace.

By hand:

```bash
git clone https://github.com/wvhhospitality/wvh-connectors.git ~/wvh-connectors
```

```bash
cd ~/wvh-connectors/slack && python3 -m venv .venv && .venv/bin/pip install -e .
```

```bash
~/wvh-connectors/slack/.venv/bin/wvh-slack-connect setup
```

```bash
~/wvh-connectors/slack/.venv/bin/wvh-slack-connect
```

Then tell Claude about it, under any name you like, such as `robin-slack` (that name is what you will see in Claude):

```bash
python3 ~/wvh-connectors/tools/claude_register.py add --name robin-slack --command ~/wvh-connectors/slack/.venv/bin/wvh-slack-mcp --env WVH_SLACK_ALLOW_WRITES=1 --ask confirm_write
```

Leave out `--env` and `--ask` for a read-only install. If Claude is open, the change waits until you quit Claude, then Claude reopens by itself: the desktop app rewrites its own settings while it runs and would undo an edit made underneath it.

## Your own Slack app

`wvh-slack-connect setup` creates a small Slack app that belongs to you. It is named for you, it lives in your Slack account, and nobody else can use it or see through it. This project's authors have no access to it.

- There is no secret to store. The app signs in with a method (called PKCE) that needs none.
- The Client ID that setup asks for is not a secret.
- Signing in ends on a small web page that hands Slack's one-time code to the connector on your own Mac. The page stores nothing and sends nothing anywhere else. Its source is in [pages/slack-connected.html](pages/slack-connected.html). If you would rather host that page yourself, put it on any HTTPS address and set `redirect_uri` in your settings before you run setup.

## Your settings

Everything personal lives in one folder, outside the app, so updating the app never touches it:

```
~/Library/Application Support/wvh-slack/
    workspaces.yaml    optional: your name, time zone, short codes, holds
    connections.json   kept by the connector: which workspaces are connected (no secrets)
    writes.jsonl       kept by the connector: a record of every post and every refusal
    downloads/         files Claude opened for you
```

Logins are not in that folder. They are in the Mac's Keychain, under the service name `WvHSlack`.

You may never need `workspaces.yaml`. Connecting a workspace records it on its own. [workspaces.example.yaml](workspaces.example.yaml) shows what you can set: your name (so Claude addresses you, not "the owner"), your time zone, a short code or nickname for a workspace, and a hold.

A **hold** (`frozen: true`) closes a workspace completely. It cannot be read or connected until you remove the line.

## Connecting and disconnecting

| Command | What it does |
|---|---|
| `wvh-slack-connect` | Go through every workspace not yet connected |
| `wvh-slack-connect northwind` | Connect one, by name or code |
| `wvh-slack-connect status` | See what is connected, waiting, and held |
| `wvh-slack-connect check NB` | Measure how fast Slack lets your app read (counts only) |
| `wvh-slack-connect disconnect NB` | Revoke the login at Slack and forget it on this Mac |
| `wvh-slack-connect setup` | One time: create your Slack app |

You can also just ask Claude: "connect Northwind."

When you leave a workspace, run `disconnect` first. If you forget, the login simply stops working when your membership ends, and Claude reports it as an expired login. Nothing breaks.

## How a post works

1. **Who and where.** For "Ask Dana," Claude looks Dana up across every workspace. If Dana is in more than one, Claude asks you which.
2. **Preview.** Claude prepares the post. Nothing is sent. The preview carries your own words as the instruction.
3. **Read back.** Claude tells you the workspace, the recipient, and the words.
4. **Confirm.** Only one tool can change Slack, and it only acts on a preview that passed every rule.

**Refused outright:**

- No instruction from you, or one that does not name this person, channel, or workspace. "Send me a note" names you, and only you: it counts when the message goes to your own account.
- The text names a different workspace than the one you named. This is the rule that keeps one company's news out of another company's channel.
- Something that looks like a password, a key, or a Social Security number.
- `@channel`, `@here`, or `@everyone` that you did not ask for.
- A recipient who is not in that workspace, or who could be two people. There is no fallback channel.
- A held workspace, or a login that answers for a different workspace than expected.

**Needs your yes first:** when the workspace was worked out rather than stated, when the channel is workspace-wide or shared with outsiders, when a sensitive subject (payroll, tax, bank details) is going to more than two people, and every file attachment.

**Also true:**

- The workspace is checked live with Slack at the preview and again at the confirm.
- A confirmation works once, for the exact action you saw, and expires in 10 minutes.
- Posting is off unless the connector is started with `WVH_SLACK_ALLOW_WRITES=1`. Leave that out and it is read-only.
- Reading never joins a channel, because joining would post a visible notice.

We also recommend setting Claude to ask for your approval every time the confirm tool is used. `--ask confirm_write` above does this.

## What Claude can do with it

Read: list workspaces, find a person across all of them, see what is new everywhere, list and search channels, read channels, threads, direct messages, files, canvases and lists, search messages and files, look up people, reactions and emoji.

Post, each one previewed first: send or schedule a message, add a reaction, start a conversation, upload a file, create or edit a canvas, create or edit a list.

Every tool takes a `workspace`, and every answer is stamped with the workspace it came from.

## Slack's limit on apps that serve several workspaces

If your app serves more than one workspace, Slack may return at most 15 messages for each history read. The connector fills the gap with search, so reading a channel still returns what you asked for, up to 100 messages. A long thread may come back short. `wvh-slack-connect check <code>` measures it for any workspace.

Slack describes apps like this one, which are not listed in its marketplace, as meant for personal and internal use. That is what this is: one app, one person, their own workspaces.

## Optional: a second Mac, or reading away from your desk

Most people need none of this.

- `wvh-slack-remote` is a read-only version for an always-on Mac. It offers no posting tools at all, it only reaches the workspaces named in `WVH_SLACK_REMOTE_REALMS`, and it saves no files. It listens on this Mac only (127.0.0.1). Reaching it from elsewhere is up to you and should sit behind its own sign-in.
- `wvh-slack-connect sync <codes>` copies logins from the Mac you sit at to that second Mac, over SSH, without a login ever touching a command line or a file. It needs a `second_mac:` block in your settings; the example file shows it.

## Updating

```bash
cd ~/wvh-connectors && git pull && slack/.venv/bin/pip install -e slack
```

Then restart Claude. Your settings and logins are untouched. Updates never install themselves.

## Removing it

Run `wvh-slack-connect disconnect <code>` for each workspace, remove the connector from Claude, delete your Slack app at api.slack.com/apps, and delete `~/wvh-connectors` and `~/Library/Application Support/wvh-slack`.

## Tests

```bash
cd ~/wvh-connectors/slack && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest
```

They run offline, with Slack and the Keychain faked. They need port 53682 free.

## License

MIT. Free to use, change and share, with no warranty. See [LICENSE](../LICENSE).
