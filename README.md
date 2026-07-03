# skills

A collection of [agent skills](https://github.com/vercel-labs/skills) for Claude Code (and other compatible coding agents).

## Skills

| Skill | Description |
| --- | --- |
| [`muscle-memory`](skills/muscle-memory/SKILL.md) | Coach mode — you write the meaningful code while the agent decomposes the task, reviews your work, and gives escalating hints instead of solutions. |

## Installation

These skills install with the [`skills`](https://www.npmjs.com/package/skills) CLI. No global install needed — run it via `npx`.

### Install everything in this repo

```bash
npx skills add alektrich/skills
```

You'll be prompted to pick which skills to install.

### Install a single skill

```bash
npx skills add https://github.com/alektrich/skills/tree/main/skills/muscle-memory
```

### Scope: project vs. global

By default skills install **project-locally** into `./.claude/skills/`, so they're committed with your repo and shared with your team. Add `-g` to install **globally** into `~/.claude/skills/`, making them available across all your projects:

```bash
npx skills add alektrich/skills -g
```

## Managing installed skills

```bash
npx skills list            # show installed skills (alias: ls)
npx skills update          # upgrade installed skills to the latest version
npx skills remove          # uninstall skills (alias: rm)
```

## Using a skill

Once installed, invoke a skill in Claude Code by name (e.g. `/muscle-memory`) or just describe what you want — the agent picks the skill up automatically from its `description`.

## Repository layout

Each skill lives in its own folder under `skills/` with a `SKILL.md` file:

```
skills/
  muscle-memory/
    SKILL.md
```

The `SKILL.md` frontmatter (`name` + `description`) is what the CLI and the agent use to discover and route to the skill.
