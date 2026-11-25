# WvH Mail

One Claude connector for every mailbox you have.

If you have more than one email address (your company's, a second company's, a personal one), this lets Claude read across all of them: what came in, what was sent, what an attachment says. When you ask, it writes a draft and leaves it in that mailbox's Drafts folder for you to read and send yourself.

**It cannot send.** There is no send, reply, forward or delete tool in this connector at all, and nothing it does marks a message as read. The most it can put into your mailbox is a draft, and only when you ask for one.

**Email is treated as data, never as instructions.** A message can say anything, and it can claim to be from anyone, including you. Claude is told plainly that nothing in an email, its hidden text or its attachments can direct it, and every answer the connector gives is marked the same way. If an email asks for something, Claude tells you what it asks and waits for your word.

## What you need

- A Mac. Passwords are kept in the Mac's Keychain, so this does not run on Windows.
- The Claude desktop app or Claude Code.
- Python 3.11 or newer, and git.
- For each mailbox, an app password. For Gmail and Google Workspace, SETUP.md walks you through making one. It takes about a minute per mailbox.

## Install

The easy way: open Claude, paste in the text of [SETUP.md](SETUP.md), and follow along. Claude does the installing. You make one app password per mailbox and paste each one into a small window on your Mac.

By hand:

```bash
git clone https://github.com/wvhhospitality/wvh-connectors.git ~/wvh-connectors
```

```bash
cd ~/wvh-connectors/mail && python3 -m venv .venv && .venv/bin/pip install -e .
```

Choose a name for this install, such as `robin-mail`. It is the name you will see in Claude, and it keeps this install's settings and passwords apart from any other.

```bash
~/wvh-connectors/mail/.venv/bin/wvh-mail-setup --install robin-mail owner Robin
```

```bash
~/wvh-connectors/mail/.venv/bin/wvh-mail-setup --install robin-mail add robin@example.com --label Studio
```

```bash
python3 ~/wvh-connectors/tools/claude_register.py add --name robin-mail --command ~/wvh-connectors/mail/.venv/bin/wvh-mail-mcp --env WVH_MAIL_INSTALL=robin-mail
```

Then quit Claude completely. It opens again by itself with the connector in place.

## How it signs in

It reads your mail the way any mail app does, over IMAP (Internet Message Access Protocol, the standard way a mail app reads a mailbox), signed in with an app password.

An app password is a separate password Google makes for one app. It is not your normal password, it works only for mail, and you can cancel it at any time at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) without changing anything else. Google requires 2-Step Verification to be on before it will make one.

`wvh-mail-setup add` asks for the app password itself: a hidden prompt in a terminal, or a small Mac window when Claude runs it. It checks the password with the mail server, then puts it straight into the Mac's Keychain. It is never typed into a chat, a command, a file, or a log, and this project's authors never see it.

Other mail providers that offer IMAP and app passwords work too: give `--imap-host` and, if needed, `--imap-port` when you add the mailbox.

## Your settings

Everything personal lives in one folder named after the install, outside the app, so updating the app never touches it:

```
~/Library/Application Support/robin-mail/
    accounts.yaml   your name, time zone, and your mailboxes (no passwords)
    drafts.jsonl    kept by the connector: one line per draft it saved (no message text)
```

Passwords are not in that folder. They are in the Mac's Keychain, under the install's name (`robin-mail`), one per mailbox address.

[accounts.example.yaml](accounts.example.yaml) shows everything you can set. You do not need to write it by hand: `wvh-mail-setup` keeps it, and makes a dated copy before every change.

| Command | What it does |
|---|---|
| `wvh-mail-setup --install robin-mail status` | What is set up, and whether each mailbox signs in |
| `wvh-mail-setup --install robin-mail owner Robin --timezone America/Chicago` | Whose mail this is, and their time zone |
| `wvh-mail-setup --install robin-mail add robin@example.com --label Studio` | Add a mailbox and store its app password |
| `wvh-mail-setup --install robin-mail password robin@example.com` | Store a new app password for a mailbox |
| `wvh-mail-setup --install robin-mail remove robin@example.com` | Take a mailbox away and forget its password |

## What Claude can do with it

| Tool | What it does |
|---|---|
| `list_accounts` | The mailboxes it can read, and whether each signs in |
| `list_unread` | Recent unread mail, in one mailbox or all of them |
| `search` | Search one mailbox; Gmail's own search words work (`from:` `newer_than:14d` `has:attachment`), and `folder: sent` searches what you sent |
| `read_message` | One message in full, with its attachments' names |
| `get_attachment` | What an attachment says: PDFs (scanned pages come back as pictures), images, text and CSV, Excel and Word |
| `append_draft` | Save a draft in that mailbox's Drafts folder, including a reply that stays in the same conversation. It is not sent. |

Every tool takes the mailbox's address or its label (`Studio`, `Personal`). With one mailbox you can leave it out.

**What it will not do:** send, reply, forward, delete, archive, star, label, mark read or unread, or save files to your Mac. A draft that replaces an earlier one moves that earlier draft to Trash, where Gmail keeps it for 30 days, and only if this connector wrote it; a draft you wrote yourself is never touched.

Sending is not included. If you later want Claude to be able to send, that is a separate, deliberate step, not a setting in this version.

## Updating

```bash
cd ~/wvh-connectors && git pull && mail/.venv/bin/pip install -e mail
```

Then restart Claude. Your settings and passwords are untouched. Updates never install themselves.

## Removing it

Run `wvh-mail-setup --install robin-mail remove <address>` for each mailbox, then `python3 ~/wvh-connectors/tools/claude_register.py remove --name robin-mail` and quit Claude. Cancel the app passwords at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords), and delete `~/Library/Application Support/robin-mail`. Delete `~/wvh-connectors` too if nothing else uses it.

## Tests

```bash
cd ~/wvh-connectors/mail && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest
```

They run offline, with the mail server and the Keychain faked.

## License

MIT. Free to use, change and share, with no warranty. See [LICENSE](../LICENSE).
