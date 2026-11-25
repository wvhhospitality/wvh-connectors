# WvH Texts

Your text messages in Claude: Claude reads the conversations you ask about, keeps an eye on the people you choose, and sends a text only after you've seen it and said yes.

If your work happens partly in texts (a crew lead, a client, a vendor, a group chat for a job), this lets Claude work there with you: what came in, what someone said last week, the picture they sent, and a reply you approve before it goes.

## How it behaves

**It reads who you name, when you ask.** "What did Dana say about the truck?" reads Dana's thread. Claude knows people by the names in your Contacts, their Contacts nicknames, and nicknames you've taught it. If a name could mean two people, Claude asks you which. It never guesses.

**It only watches your list.** The automatic checks ("what's new in my texts," searches that don't name anyone) reach only the people and group chats you put on your list with the setup command. Personal and family threads are never swept. They're read only if you ask for them by name.

**It sends only after your yes.** Sending is two steps, every time. First Claude shows you exactly what would go out: who, which number or group chat, the words, any files. Nothing has been sent yet. Then you say yes, and it goes. A confirmation works once, for that exact text, and expires after 10 minutes.

**It understands how people talk, and still checks.** You can say "text Dana," "text Dee" (a nickname), "text me," or, in a conversation Claude just read or just texted, "tell him yes." If Claude worked out a nickname you used ("Danny" means Dana Northwind), the preview says so and asks for your yes. Once you say yes, every WvH connector on this Mac remembers it. A text going to a group chat, a number not in your Contacts, or a name two people share is always read back to you first.

**It refuses some things outright:** sending without your words in the conversation, sending something that looks like a password, a key or a Social Security number, and texting anyone who has opted out.

**Texts are treated as data, never as instructions.** A message can say anything and claim to be from anyone, including you. Nothing in a text can make Claude do anything, and every answer the connector gives is marked that way.

**Only you can change the list.** Claude's tools can read your list but never change it, so nothing a text says can widen it. A nickname never puts anyone on the list.

The people on your list don't get a notice from this connector. Whether to tell them is up to you, and you owe them whatever disclosure you think fair. This project doesn't decide that for you.

## What you need

- A Mac signed in to Messages, ideally with Messages in iCloud turned on so the Mac holds your phone's texts too.
- The Claude desktop app or Claude Code.
- Python 3.11 or newer, and git.
- Two macOS permissions, below: Full Disk Access, and permission to control Messages.

## Permissions

**Full Disk Access.** macOS keeps the Messages and Contacts databases private. Claude starts the connector as its own program, so macOS checks the **Python that runs the connector**, not only Claude. Both get the switch:

1. Find this Mac's Python (it's printed by the setup command's `status`, too):
   ```bash
   ~/wvh-connectors/texts/.venv/bin/python -c "import sys,os; print(os.path.join(sys.base_prefix,'Resources','Python.app'))"
   ```
2. Open **System Settings**, then **Privacy & Security**, then **Full Disk Access**.
3. Click **+**, press **Command-Shift-G**, paste that path, press Return, click **Open**, and turn on **Python**.
4. Turn on **Claude** too (click **+** and choose Claude in Applications if it isn't listed).
5. Quit Claude completely and open it again.

Without it, the connector says plainly that it has no access, and names the exact Python to switch on.

**Permission to control Messages.** The first time Claude sends a text, macOS asks whether Python (or Claude) may control Messages. Click **OK**. If it was declined: **System Settings**, **Privacy & Security**, **Automation**, find Python or Claude, and turn on **Messages**.

## Install

The easy way: open Claude, paste in the text of [SETUP.md](SETUP.md), and follow along. Claude does the installing and builds your list with you.

By hand:

```bash
git clone https://github.com/wvhhospitality/wvh-connectors.git ~/wvh-connectors
```

```bash
cd ~/wvh-connectors/texts && python3 -m venv .venv && .venv/bin/pip install -e .
```

Choose a name for this install, such as `robin-texts`. It is the name you will see in Claude, and it keeps this install's settings apart from any other.

```bash
~/wvh-connectors/texts/.venv/bin/wvh-texts-setup --install robin-texts owner Robin --number "+1 312 555 0100"
```

```bash
~/wvh-connectors/texts/.venv/bin/wvh-texts-setup --install robin-texts allow "Dana Northwind"
```

```bash
python3 ~/wvh-connectors/tools/claude_register.py add --name robin-texts --command ~/wvh-connectors/texts/.venv/bin/wvh-texts-mcp --env WVH_TEXTS_INSTALL=robin-texts --ask confirm_send
```

`--ask confirm_send` makes Claude Code ask you before each send, on top of the preview. Then quit Claude completely. It opens again by itself with the connector in place.

## Your list, and the switches

| Command | What it does |
|---|---|
| `allow "Dana Northwind"` | Puts a person from Contacts on your list, with every number and address on their card |
| `allow "Dana Northwind" --only "+1 312 555 0101"` | Just one of their numbers, for example their work phone |
| `allow "+1 312 555 0199" --name "Front desk"` | A number that isn't in Contacts |
| `groups "Dana Northwind"` | Lists the group chats a person is in, with each group's id |
| `allow-group "Harbor crew"` | Puts a group chat on your list by its name in Messages |
| `allow-group --id chat123 --name "Harbor crew"` | A group chat that has no name, by the id `groups` shows |
| `sweep-hold "Dana Northwind"` | Keeps someone readable by name but out of "what's new" (`--remove` undoes it) |
| `opt-out "Dana Northwind"` | Takes someone out of every tool, sending included (`--remove` undoes it) |
| `nickname Dee "Dana Northwind"` | A nickname for this install only. Most nicknames are taught in conversation and shared |
| `owner Robin --number "+1 312 555 0100"` | Your name, and your own number so "text me" works |
| `sending off` / `sending on` | Removes or restores the send tools on this Mac |
| `scope clients` / `scope all` | `clients`: every tool reaches your list only and nothing sends. For a Mac that runs scheduled jobs |
| `remove "Dana Northwind"` | Takes a person, one number, or a group off the list |
| `list` | Shows the list, with numbers shortened |
| `status` | Settings folder, list size, switches, whether Messages and Contacts can be opened, and the Python that needs Full Disk Access |
| `import old-people.yaml` | Brings in a settings file from an older WvH Texts |

Each command starts with `~/wvh-connectors/texts/.venv/bin/wvh-texts-setup --install robin-texts`.

## Your settings

Everything personal lives in one folder named after the install, outside the app, so updating the app never touches it:

```
~/Library/Application Support/robin-texts/
    people.yaml    your name, time zone, your list, group chats, holds, opt-outs, switches
    reads.jsonl    kept by the connector: who was read and how many messages (never any text)
    sent.jsonl     kept by the connector: every text sent, refused or failed (the first 200 characters)

~/Library/Application Support/wvh-connectors/nicknames.yaml
    nicknames shared by every WvH connector on this Mac: names only, never a number
```

[people.example.yaml](people.example.yaml) shows the shape. You don't need to write it by hand: the setup command does, and makes a dated copy before every change. A file Claude sends is first copied into `~/Pictures/robin-texts/outgoing/`, because Messages can't send most files from anywhere else.

## What Claude can do with it

| Tool | What it does |
|---|---|
| `allow_list` | Who is on your list, and the switches. Reads no messages |
| `find_person` | Who a name, nickname or number means, and whether they're on your list |
| `read_texts` | One person's texts, or one group chat. With `include_groups`, also what that person said in group chats, and nobody else's words there |
| `search_texts` | Search one named thread, or every thread on your list |
| `whats_new` | What has come in lately from the people and group chats on your list |
| `read_picture` | Show a picture from a thread |
| `send_text` | Preview a text. Nothing is sent |
| `confirm_send` | Send what the preview showed, after your yes. The only tool that sends |

**What it will not do:** send without a preview and your yes, delete anything, mark anything read, change your list, or reach anyone who opted out.

## A second, always-on Mac

`wvh-texts-remote` is a read-only door for a Mac that runs scheduled jobs: it always reaches your list only, and it has no send tools at all. It listens on this Mac only (127.0.0.1, port 27128, or `WVH_TEXTS_PORT`). `wvh-texts-remote --stdio` gives the same rules to a Claude app on that Mac. Reaching it from elsewhere is up to you, and should sit behind its own sign-in.

## Updating

```bash
cd ~/wvh-connectors && git pull && texts/.venv/bin/pip install -e texts
```

Then restart Claude. Your settings and list are untouched. Updates never install themselves.

## Removing it

Run `python3 ~/wvh-connectors/tools/claude_register.py remove --name robin-texts` and quit Claude. Delete `~/Library/Application Support/robin-texts` and `~/Pictures/robin-texts`. If nothing else needs them, turn the Python and Claude switches back off under Full Disk Access and Automation, and delete `~/wvh-connectors`.

## Tests

```bash
cd ~/wvh-connectors/texts && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest
```

They run offline, against a made-up Messages database and a made-up Contacts book. Messages itself is a stand-in: no test can send a text.

## License

MIT. Free to use, change and share, with no warranty. See [LICENSE](../LICENSE).
