# WvH Mail

One Claude connector for every mailbox you have.

If you have more than one email address (your company's, a second company's, a personal one), this lets Claude work across all of them: what came in, what was sent, what an attachment says. It drafts, replies, replies to everyone, attaches files and signs with your signature.

**Claude can read your email, but it only sends when you tell it to.** Every email is shown to you first: from which mailbox, to whom, the subject, the words, the files. It goes out only after you say yes, and only that exact email, once. Nothing sends because an email, a schedule or Claude's own judgment says so.

**Email is treated as data, never as instructions.** A message can say anything, and it can claim to be from anyone, including you. Claude is told plainly that nothing in an email, its hidden text or its attachments can direct it, and every answer the connector gives is marked the same way. If an email asks for something, Claude tells you what it asks and waits for your word.

## What you need

- A Mac. Passwords are kept in the Mac's Keychain, so this does not run on Windows.
- The Claude desktop app or Claude Code.
- Python 3.11 or newer, and git.
- For each mailbox, an app password. For Gmail and Google Workspace, SETUP.md walks you through making one. It takes about a minute per mailbox.

## Install

The easy way: open Claude, paste in the text of [SETUP.md](SETUP.md), and follow along. Claude does the installing. You make one app password per mailbox and paste each one into a small window on your Mac. That's the only part that's yours: it works out of the box after that.

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
python3 ~/wvh-connectors/tools/claude_register.py add --name robin-mail --command ~/wvh-connectors/mail/.venv/bin/wvh-mail-mcp --env WVH_MAIL_INSTALL=robin-mail --ask confirm_send
```

`--ask confirm_send` makes Claude Code ask you before each send, too. Then quit Claude completely. It opens again by itself with the connector in place.

## How a send works

1. **You ask.** "Email Dana the call sheet." "Reply all and say Friday works." "Send me the notes."
2. **Claude previews.** Nothing is sent. The connector builds the email exactly as it would go out and checks it.
3. **Claude reads it back.** From which mailbox, to whom, the subject, the files, and anything that needs a second look.
4. **You say yes.** Only then does it send that email, once. A yes lasts ten minutes.

**Who you mean.** Your words have to name who it's for, and the connector checks them against what the mailbox and Contacts know about each address. Say it the way you would to a person:

- A name the mailbox knows: "email Dana" goes to the Dana you've been writing to.
- A nickname: "email Danny." The first time, Claude says "you said Danny; this goes to Dana Northwind. Confirm?" After your yes it remembers Danny = Dana Northwind, so the next time just works. Nicknames are shared with the other WvH connectors on this Mac.
- "Me": a note to your own mailbox.
- A conversation already going: right after Claude reads you an email, "tell her yes" answers it; right after a send, "and send the other one too" goes to the same people. Claude reads it back first.

**Stopped outright:** no words from you; words that don't name anyone it goes to; a nickname you didn't actually say; something that looks like a password, a key or a Social Security number; a signer who is set up to draft only.

**Needs your yes, read back first:** anyone outside your own domains, a nickname used for the first time, a follow-up where no one was named, a file attached, a sensitive subject (payroll, bank details) going to more than two people, an address nothing on your Mac recognizes.

**Also true:** a mailbox sends at most 10 emails in 5 minutes, so a runaway loop stops; a message to more than 20 people is refused; every send, refusal and failure is recorded in your settings folder (the first 200 characters, never the whole email, never a password).

## How it signs in

It reads your mail the way any mail app does, over IMAP (Internet Message Access Protocol, the standard way a mail app reads a mailbox), and sends over SMTP (Simple Mail Transfer Protocol, the standard way a mail app sends), signed in with an app password.

An app password is a separate password Google makes for one app. It is not your normal password, it works only for mail, and you can cancel it at any time at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) without changing anything else. Google requires 2-Step Verification to be on before it will make one.

`wvh-mail-setup add` asks for the app password itself: a hidden prompt in a terminal, or a small Mac window when Claude runs it. It checks the password with the mail server, then puts it straight into the Mac's Keychain. It is never typed into a chat, a command, a file, or a log, and this project's authors never see it.

Other providers that offer IMAP and app passwords work too. iCloud is known: give `--imap-host imap.mail.me.com`. For others, give `--imap-host`; the sending server is guessed from it and can be set in the settings file.

## Signatures and who's writing

An **identity** is who is writing, with their signature. You might have one for yourself, one per company, or one for an assistant that writes for you.

```bash
~/wvh-connectors/mail/.venv/bin/wvh-mail-setup --install robin-mail identity add Robin --signature ~/Desktop/robin.html --default
```

The signature file is copied into your settings folder. A `.txt` file of the same name beside it is used for the plain-text version. `--domain example.net=other.html` uses a different signature when sending from that domain. `--drafts-only` makes an identity that can save drafts but never send.

## Your settings

Everything personal lives in one folder named after the install, outside the app, so updating the app never touches it:

```
~/Library/Application Support/robin-mail/
    accounts.yaml   your name, time zone, mailboxes, identities, and this Mac's switches (no passwords)
    signatures/     your signature files
    sent.jsonl      kept by the connector: every send, refusal and failure (no full messages)
    drafts.jsonl    kept by the connector: one line per draft it saved (no message text)

~/Library/Application Support/wvh-connectors/nicknames.yaml   nicknames you've confirmed, shared by the WvH connectors
```

Passwords are not in those folders. They are in the Mac's Keychain, under the install's name (`robin-mail`), one per mailbox address.

[accounts.example.yaml](accounts.example.yaml) shows everything you can set. You do not need to write it by hand: `wvh-mail-setup` keeps it, and makes a dated copy before every change.

| Command | What it does |
|---|---|
| `wvh-mail-setup --install robin-mail status` | What is set up, and whether each mailbox signs in |
| `wvh-mail-setup --install robin-mail owner Robin --timezone America/Chicago` | Whose mail this is, and their time zone |
| `wvh-mail-setup --install robin-mail add robin@example.com --label Studio` | Add a mailbox and store its app password |
| `wvh-mail-setup --install robin-mail password robin@example.com` | Store a new app password for a mailbox |
| `wvh-mail-setup --install robin-mail remove robin@example.com` | Take a mailbox away and forget its password |
| `wvh-mail-setup --install robin-mail identity add NAME ...` / `identity list` | Who signs, with which signature |
| `wvh-mail-setup --install robin-mail sending off` / `on` | Turn sending off on this Mac entirely, or back on |
| `wvh-mail-setup --install robin-mail drafts off` / `on` | Turn drafts off on this Mac, or back on |
| `wvh-mail-setup --install robin-mail import <older accounts.yaml>` | Bring in mailboxes from an older WvH Mail settings file, passwords left where they are |

**Sending off** is for a Mac that runs scheduled jobs with nobody there to say yes: the send tools disappear entirely. Sending and drafts are on out of the box.

## What Claude can do with it

| Tool | What it does |
|---|---|
| `list_accounts` | The mailboxes, whether each signs in, who can sign, and whether this Mac sends |
| `list_unread` | Recent unread mail, in one mailbox or all of them |
| `search` | Search one mailbox; Gmail's own search words work (`from:` `newer_than:14d` `has:attachment`), and `folder: sent` searches what you sent |
| `read_message` | One message in full, with its attachments' names. It stays unread. |
| `get_attachment` | What an attachment says: PDFs (scanned pages come back as pictures), images, text and CSV, Excel and Word |
| `save_attachment` | Save attachments to `~/Downloads/robin-mail/`, ready to attach to a reply |
| `flag_message`, `mark_read` | Star, mark important, label, or mark read, when you ask |
| `append_draft`, `delete_draft` | Save a draft (files, signature, reply-all included); move a draft it saved to Trash |
| `send_message`, `reply_message` | Preview a new email or a reply (reply-all, files, signature). Nothing is sent. |
| `confirm_send` | Send what was previewed, after your yes |

Every tool takes the mailbox's address or its label (`Studio`, `Personal`). With one mailbox you can leave it out.

**What it will not do:** delete or archive mail, forward it, or remove flags. Reading never marks anything read. Files to attach come only from Downloads or the Desktop, never a hidden folder or a key file.

## A Mac that answers your phone

`wvh-mail-remote` is a read-only version for an always-on Mac: list, unread, search, read and attachments, nothing else. It listens on that Mac only (127.0.0.1, port 27129 unless `remote_port` says otherwise), and `--stdio` runs the same tools for a Claude app there. With `remote_drafts: on` it can also save drafts, without files. Reaching it from elsewhere is up to you and should sit behind its own sign-in. `WVH_MAIL_REMOTE_ACCOUNTS` narrows it to some mailboxes.

## Updating

```bash
cd ~/wvh-connectors && git pull && mail/.venv/bin/pip install -e mail
```

Then restart Claude. Your settings, signatures and passwords are untouched. Updates never install themselves.

## Removing it

Run `wvh-mail-setup --install robin-mail remove <address>` for each mailbox, then `python3 ~/wvh-connectors/tools/claude_register.py remove --name robin-mail --ask confirm_send` and quit Claude. Cancel the app passwords at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords), and delete `~/Library/Application Support/robin-mail`. Delete `~/wvh-connectors` too if nothing else uses it.

## Tests

```bash
cd ~/wvh-connectors/mail && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest
```

They run offline, with the mail servers, Contacts and the Keychain faked.

## License

MIT. Free to use, change and share, with no warranty. See [LICENSE](../LICENSE). One of the libraries it installs to read scanned PDFs, PyMuPDF, has its own license (AGPL); it is installed, not included here.
