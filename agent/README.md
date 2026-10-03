# Mac agent

Collects your Firefox and Safari history (Safari includes iPhone visits synced
by iCloud) and your own Claude Code prompts, drops anything your never-collect
list forbids, and keeps the rest in a local outbox
(`~/Library/Application Support/curator-agent/outbox.sqlite`) until it is sent
to the curator. Titles and URLs only, never page contents.

    uv run curator-agent status
    uv run curator-agent pause 2h
    uv run curator-agent forget example.com

## The never-collect list

Yours lives on your Mac, at
`~/Library/Application Support/curator-agent/never-collect.yaml`, created from
`never-collect.example.yaml` the first time the agent runs. Edit it freely.
Each upload carries its domains and patterns, so the curator purges history
they now forbid; `forget` adds a domain and purges it there straight away.

Log: `~/Library/Logs/curator-agent.log` (counts only, no URLs).

## Install

    uv sync
    sed "s|__HOME__|$HOME|g; s|__REPO__|$PWD|g; s|__CURATOR_URL__|https://curator.example.org|" \
        agent/launchd/curator-agent.plist.example > ~/Library/LaunchAgents/local.curator-agent.plist
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.curator-agent.plist

Then in the reader app, Settings → Mac agent → Make an agent token, and put it
in the keychain: `security add-generic-password -U -s curator -a CURATOR_AGENT_TOKEN -w`.

Safari's history needs **Full Disk Access** for the Python that runs the agent:
System Settings → Privacy & Security → Full Disk Access, and drag in the file
`realpath .venv/bin/python` names (the file picker greys it out). Re-grant
after `uv python upgrade`. Safari on the Mac must be opened now and then for
iCloud to merge the iPhone's history into `History.db`.
