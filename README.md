A collection of agent skills for Claude Code (and other compatible coding agents).

## Skills

| Skill                                            | Description                                                                                                                                         |
| ------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`i-read-code`](skills/i-read-code/SKILL.md)     | Manual review of your branch (`/i-read-code`). Diffs against main/master or any ref, explains each change, and opens a local page where you Confirm or Decline each one with connected files alongside. Decisions are saved to JSON and the agent offers to fix what you declined. Needs `python3`. |
| [`muscle-memory`](skills/muscle-memory/SKILL.md) | Coach mode — you write the meaningful code while the agent decomposes the task, reviews your work, and gives escalating hints instead of solutions. |
| [`unslop`](skills/unslop/SKILL.md)               | Cut AI tells from any writing. A numbered rulebook of patterns to detect and rewrite; other skills (like `whiplash`) invoke it.                                        |
| [`whiplash`](skills/whiplash/SKILL.md)           | Demanding-mentor mode (`/whiplash`). Challenges bad ideas and sloppy reasoning, makes you think before it answers, and only praises work that earned it. Writes in `unslop` style. |

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

Each skill lives in its own folder under `skills/` with a `SKILL.md` file, plus any scripts or assets it bundles:

```
skills/
  i-read-code/
    SKILL.md
    scripts/      # diff collection, page build, local server
    assets/       # review page template
    tests/        # python3 -m unittest discover -s skills/i-read-code/tests
  muscle-memory/
    SKILL.md
  unslop/
    SKILL.md
  whiplash/
    SKILL.md
```

The `SKILL.md` frontmatter (`name` + `description`) is what the CLI and the agent use to discover and route to the skill.
