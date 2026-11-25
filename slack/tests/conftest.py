"""Shared test setup. Every name in these tests is made up."""

import os

# The server reads its owner once, when it starts, so this has to be set before it loads.
os.environ.setdefault("WVH_SLACK_OWNER", "Robin")
