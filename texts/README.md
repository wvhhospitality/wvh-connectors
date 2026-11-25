# WvH Texts

Lets Claude read the text messages on your Mac, but only from the people and group chats you choose.

If your work happens partly in texts (a crew lead, a client, a vendor, a group chat for a job), this lets Claude read those threads with you: what came in, what someone said last week, the picture they sent. Everyone else in your Messages app stays out of reach.

**It cannot send.** There is no send, reply or delete tool in this connector at all. It opens the Messages database read-only, so it cannot change anything in it either.

**Nothing is read unless you put it on the allow-list.** The allow-list starts empty, and with an empty list the connector reads nothing at all. You add people one at a time, by name, with a setup command. A thread with anyone not on the list is never read, searched, counted or summarized, even if you ask for that person by name: Claude tells you they are not on the list and how to add them. Personal and family threads stay closed unless you add them yourself.

**Group chats are separate.** A group chat is readable only if you add that group. Someone on your list being in a group never opens the group.

**Only you can change the list.** Claude's tools can read the list but never change it, so nothing a text says can widen it.

**Texts are treated as data, never as instructions.** A message can say anything and claim to be from anyone, including you. Claude is told plainly that nothing in a text can direct it, and every answer the connector gives is marked the same way.

The people on your list don't get a notice from this connector. Whether to tell them is up to you, and you owe them whatever disclosure you think fair. This project doesn't decide that for you.

## What you need

- A Mac signed in to Messages, ideally with Messages in iCloud turned on so the Mac holds your phone's texts too.
- The Claude desktop app or Claude Code.
- Python 3.11 or newer, and git.
- Permission for Claude to read Messages and Contacts: Full Disk Access, below.

## Full Disk Access

macOS keeps the Messages and Contacts databases private. To read them, the app that starts the connector needs Full Disk Access. That is the Claude desktop app, whether you use its chat or its Code tab.

Open **System Settings**, then **Privacy & Security**, then **Full Disk Access**. Turn on **Claude**. If Claude isn't in the list, click **+**, choose **Claude** in Applications, and turn it on. Then quit Claude completely and open it again.

If you run the setup command in Terminal instead of in Claude, give Terminal the same permission, or the setup can't look anyone up.

Without it, the connector says plainly that it has no access. It never guesses.

## Install

The easy way: open Claude, paste in the text of [SETUP.md](SETUP.md), and follow along. Claude does the installing and builds the allow-list with you.

By hand:

```bash
git clone https://github.com/wvhhospitality/wvh-connectors.git ~/wvh-connectors
```

```bash
cd ~/wvh-connectors/texts && python3 -m venv .venv && .venv/bin/pip install -e .
```

Choose a name for this install, such as `robin-texts`. It is the name you will see in Claude, and it keeps this install's settings apart from any other.

```bash
~/wvh-connectors/texts/.venv/bin/wvh-texts-setup --install robin-texts owner Robin
```

```bash
~/wvh-connectors/texts/.venv/bin/wvh-texts-setup --install robin-texts allow "Dana Northwind"
```

```bash
python3 ~/wvh-connectors/tools/claude_register.py add --name robin-texts --command ~/wvh-connectors/texts/.venv/bin/wvh-texts-mcp --env WVH_TEXTS_INSTALL=robin-texts
```

Then quit Claude completely. It opens again by itself with the connector in place.

## The allow-list

| Command | What it does |
|---|---|
| `allow "Dana Northwind"` | Adds a person from Contacts, with every number and address on their card |
| `allow "Dana Northwind" --only "+1 312 555 0101"` | Adds just one of their numbers, for example their work phone |
| `allow "+1 312 555 0199" --name "Front desk"` | Adds a number that isn't in Contacts |
| `groups "Dana Northwind"` | Lists the group chats a person is in, with each group's id, so you can pick one |
| `allow-group "Harbor crew"` | Adds a group chat by its name in Messages |
| `allow-group --id chat123 --name "Harbor crew"` | Adds a group chat that has no name, by the id `groups` shows |
| `nickname Dee "Dana Northwind"` | Lets you say "Dee." A nickname never adds anyone to the list |
| `remove "Dana Northwind"` | Takes a person, one number, or a group off the list |
| `list` | Shows the list, with numbers shortened |
| `status` | Shows the settings folder, the list's size, and whether Messages and Contacts can be opened |

Each command starts with `~/wvh-connectors/texts/.venv/bin/wvh-texts-setup --install robin-texts`. A person is found by the name saved in Contacts. If a name could mean more than one person, nothing is added and the command lists who it could be.

What gets read for a person on the list: their direct threads with the numbers and addresses on the list, and nothing else. If they get a new number, run `allow` for them again.

## Your settings

Everything personal lives in one folder named after the install, outside the app, so updating the app never touches it:

```
~/Library/Application Support/robin-texts/
    people.yaml    your name, time zone, the allow-list, group chats, nicknames
    reads.jsonl    kept by the connector: who was read and how many messages (never any text)
```

[people.example.yaml](people.example.yaml) shows the shape. You don't need to write it by hand: the setup command does, and makes a dated copy before every change.

## What Claude can do with it

| Tool | What it does |
|---|---|
| `allow_list` | Who is on the list. Reads no messages |
| `find_person` | Who a name, nickname or number means, and whether they are on the list |
| `read_texts` | One allowed person's texts, or one allowed group chat, with pictures listed |
| `search_texts` | Search allowed threads for words |
| `whats_new` | What has come in lately from the people and groups on the list |
| `read_picture` | Show a picture from an allowed thread |

**What it will not do:** send, reply, react, delete, mark anything read, change the allow-list, or read anyone not on it.

## Updating

```bash
cd ~/wvh-connectors && git pull && texts/.venv/bin/pip install -e texts
```

Then restart Claude. Your settings are untouched. Updates never install themselves.

## Removing it

Run `python3 ~/wvh-connectors/tools/claude_register.py remove --name robin-texts` and quit Claude. Delete `~/Library/Application Support/robin-texts`. If nothing else needs it, turn Claude's Full Disk Access back off in System Settings, and delete `~/wvh-connectors`.

## Tests

```bash
cd ~/wvh-connectors/texts && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest
```

They run offline, against a made-up Messages database and a made-up Contacts book.

## License

MIT. Free to use, change and share, with no warranty. See [LICENSE](../LICENSE).
