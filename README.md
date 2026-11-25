# WvH Connectors

Free connectors that let Claude work across the accounts a small business owner really has: several Slack workspaces, several mailboxes, and so on. Built by WvH Hospitality LLC in Southern California, and used in our own practice every day.

Each connector runs on your own Mac. Your logins stay in your Mac's Keychain, your settings stay in a folder of your own, and nothing is sent to us.

| Connector | What it does | Status |
|---|---|---|
| [slack](slack/) | One connector for every Slack workspace you belong to. Reads everything you can see; posts when you tell it to. | Ready |
| [mail](mail/) | One connector for every mailbox you have. Reads, searches and opens attachments; drafts, replies (reply-all too) and sends with attachments when you tell it to. | Ready |
| [texts](texts/) | Reads the texts on your Mac for anyone you name; sends a text or a picture when you tell it to. Its automatic check-ins cover only the people you list. | Ready |

## One habit for everything that leaves your Mac

Claude reads freely. Nothing goes out until you've seen it and said yes.

1. You say it in your own words: "Email Max the call sheet," "Slack Danny that we're on," "Text Robin I'm five minutes out."
2. Claude prepares it and shows you exactly who, where and what. Nothing has been sent.
3. You say yes. Only then does it go.

A message, email or Slack post can never tell Claude to send something: they're information, not instructions, whoever they claim to be from.

**It knows who you mean.** Teach a nickname once ("Danny" is Dana Northwind) and all three connectors know it. In a conversation already going, "tell him yes" just continues it. Either way, the preview shows exactly where it's going before your yes.

**Turning sending off.** Everything works out of the box. A Mac that runs scheduled jobs on its own should never send: put `sending: off` in that connector's settings file and its sending tools disappear.

## How they are built

- **The app and your settings are kept apart.** The app is in this repository. Everything personal lives in `~/Library/Application Support/` on your Mac. Updating the app never touches your settings, and your settings can never end up here.
- **Each person gets their own.** A connector is set up under your name, with your own accounts. We have no access to anything you connect.
- **Nothing updates by itself.** You get a new version when you ask for it.
- **Careful by default.** Reading is open. Anything that sends or changes something is shown to you first and waits for your yes.

## Installing

Open the connector's folder and follow its `SETUP.md`: you paste one page into Claude and it does the work.

## Updating

One line updates every connector you have installed. Your settings, nicknames and logins aren't touched:

```bash
cd ~/wvh-connectors && git pull && for c in slack mail texts; do [ -d $c/.venv ] && $c/.venv/bin/pip install -q -e $c; done
```

Then quit and reopen Claude. Updates never install themselves.

## License

MIT. Free to use, change and share, with no warranty. See [LICENSE](LICENSE).
