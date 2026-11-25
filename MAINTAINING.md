# Maintaining this repository

For whoever publishes changes here.

## Before every commit

```bash
slack/.venv/bin/python tools/privacy_check.py
```

(Any connector's own Python will do: the check needs PyYAML, and stops rather than run half-blind without it.) It reads the real settings on your Mac, which never live in this repository, and refuses to pass if any name, address, phone number or workspace id from them appears in a file git would publish. It also catches anything shaped like a password, a key, a private network address or a real home folder path.

Make it automatic, once per clone:

```bash
python3 tools/privacy_check.py --install
```

After that it runs before every commit and every push from this Mac.

## Rules

- Settings never live here. Each connector reads `~/Library/Application Support/<its folder>/`.
- Every name in the tests, examples and docs is made up. If a test needs a company, invent one.
- Nothing personal is written into the code. A person's name, time zone or second computer is a setting.
- A change that alters how a connector behaves needs a test.

## Before the first publish

- The install address in `slack/README.md` and `slack/SETUP.md` assumes this repository lives at `github.com/wvhhospitality/wvh-connectors`. Change both if it does not.
- `DEFAULT_REDIRECT_URI` in `slack/src/wvh_slack_mcp/config.py` assumes GitHub Pages serves this repository from the main branch at `wvhhospitality.github.io/wvh-connectors/`. Turn Pages on, open the address, and confirm the sign-in page loads before telling anyone to run setup. An install records the address it was set up with, so changing the default later does not break existing installs.

## Releasing

Everyone runs the same version: the practice, its team and its clients. A release is:

1. Make the change in this clone, with a test for any change in behavior.
2. Bump `version` in that connector's `pyproject.toml`.
3. Run its tests, then the privacy check (it also runs on every commit and push).
4. Commit, tag `<connector>-vX.Y.Z` (for example `slack-v0.4.0`), and push `main` and the tag.
5. Each Mac updates with the one line in the README's "Updating" section, then restarts Claude.

A bad release is rolled back by checking out the previous tag on the Macs that need it.
