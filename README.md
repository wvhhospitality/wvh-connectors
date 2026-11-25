# WvH Connectors

Free connectors that let Claude work across the accounts a small business owner really has: several Slack workspaces, several mailboxes, and so on. Built by WvH Hospitality LLC in Southern California, and used in our own practice every day.

Each connector runs on your own Mac. Your logins stay in your Mac's Keychain, your settings stay in a folder of your own, and nothing is sent to us.

| Connector | What it does | Status |
|---|---|---|
| [slack](slack/) | One connector for every Slack workspace you belong to. Reads freely, posts only on your command. | Ready |
| [mail](mail/) | One connector for every mailbox you have. Reads, searches and reads attachments; saves drafts for you to send. It cannot send. | Ready |
| [texts](texts/) | Reads the text messages on your Mac, only from the people and group chats you allow. It cannot send. | Ready |

## How they are built

- **The app and your settings are kept apart.** The app is in this repository. Everything personal lives in `~/Library/Application Support/` on your Mac. Updating the app never touches your settings, and your settings can never end up here.
- **Each person gets their own.** A connector is set up under your name, with your own accounts. We have no access to anything you connect.
- **Nothing updates by itself.** You get a new version when you ask for it.
- **Careful by default.** Reading is open. Anything that sends or changes something is shown to you first.

## Installing

Open the connector's folder and follow its `SETUP.md`: you paste one page into Claude and it does the work.

## License

MIT. Free to use, change and share, with no warranty. See [LICENSE](LICENSE).
