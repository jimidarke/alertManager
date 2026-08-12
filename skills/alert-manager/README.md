# alert-manager (Claude Code skill)

A [Claude Code Agent Skill](https://code.claude.com/docs/en/skills) that
teaches an LLM agent how to integrate with this project's Alert Manager
service: sending alerts, choosing the right integration path (SDK vs. raw
HTTP vs. the turnkey Linux host-health agent), provisioning credentials, and
recognizing the gotchas that produce silent failures. See `SKILL.md` for
what Claude actually reads.

This directory is tracked in the main repo so it ships with the project for
anyone using it — but living here isn't the same as being *active*. Claude
Code only auto-loads skills from a project's own `.claude/skills/` or from
`~/.claude/skills/` (personal, every project on the machine). Installing
means copying this directory into one of those two locations, which is what
`install.sh` does.

## Install / update

From a local checkout of this repo:

```bash
bash skills/alert-manager/install.sh
```

Or standalone, no checkout needed:

```bash
curl -fsSL https://raw.githubusercontent.com/jimidarke/alertManager/main/skills/alert-manager/install.sh | bash
```

Both default to installing into `~/.claude/skills/alert-manager` (personal,
available to every Claude Code session on the machine). Override the
destination with `CLAUDE_SKILLS_DIR` — for example, to install into a single
project instead of system-wide:

```bash
CLAUDE_SKILLS_DIR=/path/to/your/project/.claude/skills bash skills/alert-manager/install.sh
```

**Updating is the same command.** `install.sh` is idempotent — it always
overwrites the destination with the current source. There's no separate
`update` subcommand and no background auto-updater; re-run the exact command
above whenever you want the latest version. This is a deliberate choice, not
a missing feature — nothing about this skill's instructions should change
underneath a running agent without a human explicitly asking for the update,
and it matches how the rest of this project already documents "how do I get
the latest" (e.g. the Python SDK's own upgrade instructions are a plain
`pip install --upgrade` command, not a background updater either).

## Testing this skill before you trust it

This skill was developed and exercised inside this repo before being
published:

1. Symlinked into this repo's own (gitignored) `.claude/skills/alert-manager`
   so it's active for project-scoped Claude Code sessions here without
   running the installer.
2. `install.sh` was run against scratch destinations to verify both code
   paths (local-checkout copy, and the standalone sparse-clone fetch) —
   see the commit history / conversation this skill was built in for the
   exact verification steps.
3. `scripts/send-alert.sh`'s local validation was exercised directly (bad
   level/category/empty-field rejection, all without a network call) and
   its `--health` check was run against the real, live deployment.
4. A handful of realistic prompts (e.g. "send a critical alert if this
   deploy fails", "set up health monitoring for this box", "I'm getting a
   403 on POST /alert") were run through an agent with this skill available
   to confirm it reaches for the right tool and surfaces the right gotchas
   unprompted.

If you're modifying this skill, repeat a version of that loop rather than
assuming a documentation-only change is risk-free — the gotchas section in
particular is easy to get subtly wrong in a way that only shows up when an
agent actually tries to act on it.

## Structure

```
alert-manager/
├── SKILL.md                     # what Claude Code reads when the skill triggers
├── install.sh                   # copies this dir to ~/.claude/skills/alert-manager
├── VERSION                      # this skill package's own version
├── references/                  # detail SKILL.md points into, not inlined
│   ├── payload-reference.md
│   ├── auth-and-provisioning.md
│   ├── sdk-python.md
│   └── linux-agent.md
└── scripts/
    └── send-alert.sh            # portable sender with built-in local validation
```

## Keeping this in sync with the actual service

`SKILL.md` and the `references/` files are a curated snapshot of
`docs/CLIENTNODE.md`, `docs/AUTH.md`, the SDK source, and
`clients/linux-agent/README.md` elsewhere in this repo — not a generated
copy. If the wire contract, auth model, or SDK surface changes, update both
the source docs and this skill; they're allowed to drift in wording but not
in the facts they state. `SKILL.md` itself points agents at
`GET /docs` on a live deployment (or the GitHub-hosted `CLIENTNODE.md` if
they don't have credentials yet) as the actual escape hatch when in doubt —
this skill is meant to get an agent correct on the first try, not to be the
final word if it and the live service ever disagree.
