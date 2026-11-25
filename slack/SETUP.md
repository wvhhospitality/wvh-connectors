# Set up WvH Slack

Paste everything below the line into Claude (the desktop app's Code tab, or Claude Code) on the Mac where you use Slack. Claude does the work and tells you when it needs a click.

---

You are setting up the WvH Slack connector on this Mac for the person you are talking with. It gives you one set of Slack tools that works across every Slack workspace they belong to. Go one step at a time, say what you are doing in plain words, and stop to ask whenever a step says to.

Rules for the whole setup:

- Never ask for, type, or read a Slack password. The person signs in to Slack themselves, in their own browser.
- Never put a login or token in a file, a command, or this chat. The connector stores logins in the Mac's Keychain by itself.
- If a step fails, show the error and stop. Do not improvise a different way to install.
- Before you change any existing file, make a copy of it beside the original with `.bak` and today's date on the end.

## 1. Check the Mac

Confirm each of these, and stop with a plain explanation if one is missing:

- This is a Mac (`uname` says Darwin).
- `python3 --version` is 3.11 or newer.
- `git --version` works.
- The Slack desktop app is installed and the person is signed in to their workspaces in it. Ask them to confirm.

## 2. Get the connector

If `~/wvh-connectors` does not exist:

```bash
git clone https://github.com/wvhhospitality/wvh-connectors.git ~/wvh-connectors
```

If it already exists, update it instead:

```bash
git -C ~/wvh-connectors pull
```

Then install it into its own private Python environment:

```bash
cd ~/wvh-connectors/slack && python3 -m venv .venv && .venv/bin/pip install -e .
```

## 3. Ask three questions

1. "What first name should I use for you?" Claude will say this name whenever it talks about their Slack.
2. "What would you like this connector to be called in Claude?" Offer a default built from their name or company, lowercase with a hyphen, ending in `-slack`. For example `robin-slack`. Letters, numbers and hyphens only.

Then ask one more:

3. Tell them, in these words or close: "Claude can post in Slack when you tell it to. Every post is shown to you first, who, where and the words, and goes only after your yes." If they'd rather it only read, note that for step 6.

## 4. Save their name

Create the settings folder and write their name. Do not overwrite a `workspaces.yaml` that already exists: add the `owner_name` line to it instead.

```bash
mkdir -p ~/Library/Application\ Support/wvh-slack
```

The file `~/Library/Application Support/wvh-slack/workspaces.yaml` needs this one line, with their name:

```yaml
owner_name: Robin
```

## 5. Create their Slack app and connect

Tell the person: "Slack is about to open with your own app already filled in. I will tell you the clicks." Then run:

```bash
~/wvh-connectors/slack/.venv/bin/wvh-slack-connect setup
```

It prints the steps. Read them to the person. They finish by copying a Client ID from Slack and giving it to you. The Client ID is not a secret. Save it:

```bash
~/wvh-connectors/slack/.venv/bin/wvh-slack-connect setup --client-id PASTE_THE_ID_HERE
```

Now see which workspaces this Mac is signed in to:

```bash
~/wvh-connectors/slack/.venv/bin/wvh-slack-connect status
```

Read the "To connect" list to the person and ask which ones they want. Connect only those, one at a time, by name. Tell them to click **Allow** when their browser opens, then run:

```bash
~/wvh-connectors/slack/.venv/bin/wvh-slack-connect "WORKSPACE NAME"
```

If it says a different workspace was approved than the one asked for, their browser was signed in to another workspace. Ask them to open the right workspace in that browser, then run it again.

Run `status` once more and confirm the ones they chose are under "Connected."

## 6. Tell Claude about the connector

Use the name from step 3. One command registers it with both the Claude desktop app and Claude Code, keeps every other connector exactly as it was, and makes a dated `.bak` copy of each settings file first.

Posting is on, with every post previewed and waiting for their yes:

```bash
python3 ~/wvh-connectors/tools/claude_register.py add --name robin-slack --command ~/wvh-connectors/slack/.venv/bin/wvh-slack-mcp --ask confirm_write
```

Only if they asked for read only, add `--env WVH_SLACK_ALLOW_WRITES=0` to that line.

Replace `robin-slack` with their name from step 3. Do not edit Claude's settings files by hand.

**Why it may wait.** The Claude desktop app keeps its settings in memory and writes them back while it runs and when it quits, so a change made while it is open is quietly undone. If Claude is open (it is, if you are running inside it), the command does not change anything yet. It waits in the background, makes the change the moment Claude closes, and opens Claude again by itself.

## 7. Before they quit, tell them what they have

The chat ends when Claude quits, so say this first, in a few lines:

- what the connector is called and which workspaces are connected,
- that they can say "what's new in Slack," "read #channel in Northwind," or "ask Dana whether Friday works,"
- that every post is previewed first and needs their yes (or that it only reads, if they chose that),
- that nicknames work: if they say "Slack Danny" and Claude knows Danny is Dana, the preview shows "Danny means Dana Northwind" once, and their yes teaches it to Slack, mail and texts for good,
- that in a conversation already going, "tell him yes" just works: the preview names the conversation it's continuing,
- that their settings are in `~/Library/Application Support/wvh-slack`, and their logins are in the Mac's Keychain,
- that "update my Slack connector" will fetch the newest version.

If you keep notes or memory for this person, save one line now: use the tools named in step 3 for all Slack work, name the workspace next to anything you report, and never post unless they ask in the chat.

## 8. Restart and check

Tell the person, in these words or close to them: "Last step. Quit Claude completely: the Claude menu, then Quit Claude, or Command-Q. It opens again by itself a few seconds later. Then start a new chat and say: list my Slack workspaces."

In the new chat, the connector's `list_workspaces` tool should answer, and every workspace they connected should say `ok`.

If the Slack tools are missing in the new chat, paste this page again and say "check step 8." Then run:

```bash
python3 ~/wvh-connectors/tools/claude_register.py status --name robin-slack
```

It should say registered in both, and not waiting. `~/Library/Logs/wvh-connectors-register.log` records what the waiting step did. If Claude was not quit within an hour of step 6, nothing changed: run step 6 again, then quit Claude.

## Updating later

When the person says "update my Slack connector":

```bash
cd ~/wvh-connectors && git pull && slack/.venv/bin/pip install -e slack
```

Then ask them to restart Claude. Their settings and logins are not touched.
