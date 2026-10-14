---
name: i-read-code
description: Interactive manual code review of a branch. Diffs the current branch (committed plus uncommitted work) against main/master or any ref the user names, writes a plain-language explanation of every meaningful change, and opens a local web page where the user steps through the files, sees connected files (callers, tests, imported modules, even unchanged ones) on the right, and clicks Confirm or Request Change (with a reason) on each change. Decisions are saved to a local JSON file, then Claude summarizes them and offers to fix the declined ones. Use when the user types /i-read-code, or wants to read, walk through, sign off on, or manually review their own branch, PR or AI-written changes line by line instead of getting an automated review.
---

# i-read-code

The user wants to **read** the change themselves, not hand the review off to you. Your job is to make that fast and thorough: explain each change so they understand it without reverse-engineering the diff, point them to the code it touches, and record their verdicts. You are a guide here. Your explanations say what the code does and what to look at; the user decides whether it's right.

Three bundled scripts do the mechanical work (standard library Python 3, no installs). `SKILL_DIR` below means the directory containing this file.

| Script | Does |
| --- | --- |
| `scripts/collect_changes.py` | Finds the base, computes the diff, writes `changes.json` and a line-numbered `diff.txt` |
| `scripts/build_review.py` | Validates your `analysis.json`, fills gaps, writes `index.html` + `data.json` |
| `scripts/serve.py` | Serves the page on 127.0.0.1, writes every decision to `review.json`, and answers questions from the page with a fork of this session |

## 0. Pick the Python command

The interpreter is named differently across systems. macOS and most Linux distros only ship `python3`. On Windows the usual names are `python` or `py -3`, and `python3` is often a Store stub. Some older machines have `python` pointing at Python 2. Find one that is really Python 3:

```bash
for c in python3 python "py -3"; do $c -c 'import sys; assert sys.version_info[0] == 3' 2>/dev/null && { echo "$c"; break; }; done
```

Use the printed command wherever `PY` appears below, for example `python3 "$SKILL_DIR/scripts/..."`. Shell variables don't carry over between Bash calls, so write the command out each time.

If nothing prints, there's no Python 3. Stop and tell the user. Do **not** install it yourself: it's a system-level change. Suggest the command for their OS and run it only if they say yes:

- macOS: `brew install python` (with Homebrew), or `xcode-select --install`
- Debian/Ubuntu: `sudo apt install python3`
- Fedora/RHEL: `sudo dnf install python3`
- Windows: `winget install Python.Python.3.12`

After an install, run the check again.

## 1. Collect the changes

Work out the comparison from the user's request:

- Nothing said → default. The script tries `main`, `master`, `origin/main`, `origin/master` in that order and compares `merge-base(base, HEAD)` against the **working tree**, so committed, staged, unstaged and untracked changes are all included.
- "against develop", "vs v2.3", "since abc123" → `--base <ref>`.
- "only what's committed" → `--committed-only`.
- A different branch or commit to review, not the checkout → `--head <ref>` (committed content only).

```bash
PY "$SKILL_DIR/scripts/collect_changes.py" [--base REF] [--head REF] [--committed-only]
```

Run it from inside the repo. It prints the output directory (`<repo>/.i-read-code/<branch>/`), the file list with +/- counts, and notes such as "you're on the base branch". It also adds `.i-read-code/` to `.git/info/exclude`, so review files never show up in `git status`.

If it says **"Existing review found"**: when the diff is unchanged and items are still pending, ask whether to resume. To resume, skip to step 4 and run `serve.py` on that directory; saved decisions load automatically. Otherwise continue. Rebuilding archives the old `review.json` to `history/`.

If there are no changes, or the base can't be found, tell the user and stop.

## 2. Understand the change, then write `analysis.json`

Read `diff.txt` completely; use `offset`/`limit` for long ones. Every diff line carries OLD and NEW line numbers, so copy numbers from there rather than counting. Then do what a good reviewer would do before explaining anything:

- Open the full changed files where the hunks alone don't make the logic clear.
- For each changed function, type, endpoint, config key or schema, search (`grep`/`rg`) for its **callers and users**, including files that didn't change. These unchanged files are often where a bug surfaces, and they become the context files on the right side of the review page.
- Find the tests that exercise the change, or notice that there are none.

Then write `<review-dir>/analysis.json`:

```json
{
  "overview": "2–5 sentences: what the branch does as a whole and how the pieces fit. Name the riskiest part.",
  "review_order": ["src/models/user.py", "src/api/users.py", "tests/test_users.py"],
  "files": {
    "src/api/users.py": { "summary": "One or two sentences on this file's role in the change." }
  },
  "changes": [
    {
      "id": "c1",
      "file": "src/api/users.py",
      "side": "new",
      "start": 42,
      "end": 67,
      "title": "Paginate GET /users with cursor tokens",
      "importance": "high",
      "explanation": "Replaces offset pagination with an opaque cursor (`base64(id)`). The handler now fetches `limit + 1` rows to know whether a next page exists.\n\nClients that sent `?page=` get the first page silently instead of an error.",
      "concerns": "`limit` isn't capped, so `?limit=100000` loads the whole table."
    }
  ],
  "connections": {
    "src/api/users.py": [
      { "path": "src/models/user.py", "reason": "Calls the new `User.after_cursor()` query." },
      { "path": "web/src/hooks/useUsers.ts", "reason": "Frontend caller; still reads `page` from the response.", "lines": [[12, 30]] }
    ]
  }
}
```

The schema, field by field:

- **changes**: the review items. Each one is a contiguous line range in **one** file, and gets its own Confirm/Request Change. Make one item per logical change. Split a hunk that does two unrelated things. Merge adjacent hunks that make one point. Every non-trivial changed line should belong to an item; the build adds "Change without an explanation" items for anything you skip, and those are a failure of your analysis.
  - `side`: `"new"` with new-file line numbers (the normal case, including modifications that remove lines in between). `"old"` with old line numbers for pure removals. `"file"`, with no start/end, for a whole-file point such as a new file explained as a unit, a deleted file, or a rename.
  - `importance`: `high` for behavior, security, data or API contracts; `medium` for meaningful logic or structure; `low` for renames, formatting, comments, small refactors. Trivial edits can be grouped into one `low` item per file, but they still get an item so the user can sign off.
  - `explanation`: **what changed and what it means**. Cover intent, the behavior difference compared with before, and effects on callers. Don't narrate the syntax ("adds an if statement"); the user can see the code. Write the way you'd brief a colleague who is about to read it. Supports blank-line paragraphs, `- ` bullets, `` `code` `` and `**bold**`.
  - `concerns` (optional): specific things worth checking, such as an unhandled case, a caller that wasn't updated, or a missing test. Only real, specific doubts. Don't decline for the user. If a concern is serious, say it plainly, and the user decides.
- **connections**: for each changed file, the files a reviewer should look at alongside it. The value is a list of `{path, reason, lines?}`. Link changed files to each other (the build mirrors links) and to **unchanged** files that call, are called by, test, configure or define the types used by the change. `reason` should say *why*, in terms of this change. `lines` (optional, for context files) are the ranges to highlight. Aim for the 2–6 links that matter, not every import.
- **files**: per-file one-line summaries. **review_order**: a sensible reading order, usually data model → logic → API → UI → tests. Generated files go last automatically.

Lock files, build output and binaries get automatic items. Mention them in an item only if they matter, for example a dependency upgrade inside a lock file.

**Big branches.** If `diff.txt` has more than about 3,000 lines across many files, split the files into groups by area and have subagents each write `analysis-<group>.json` with the same schema. Give each one the review directory path, its file list, and this section of the instructions. Write `analysis.json` yourself with the overview, order and cross-group connections. The build merges every `analysis*.json` in the directory. Change ids must be unique across files, so prefix them by group.

## 3. Build the page

```bash
PY "$SKILL_DIR/scripts/build_review.py" --dir <review-dir>
```

If it exits with problems (unknown file, missing title or explanation, bad line numbers), fix `analysis.json` and rerun. Read the warnings too. A "touches no changed line" warning usually means you cited the wrong side or an off-by-some range. A list of uncovered lines means you skipped something. Add those items and rebuild; aim for zero uncovered lines.

## 4. Serve it and hand over to the user

Start the server **in the background** (Bash with `run_in_background: true`), because it runs until the user finishes:

```bash
PY "$SKILL_DIR/scripts/serve.py" --dir <review-dir>
```

It opens the browser and prints the URL. Start it from this session's Bash tool rather than having the user run it: the server reads this session's id from `CLAUDE_CODE_SESSION_ID`, saves it to `review.json` as `session_id`, and answers questions from the page with `claude -p` **forked from this session**, so the answers know everything you learned writing the analysis. Its third line of output says which session it forks, or that questions are off because `claude` isn't on PATH. If the env var is missing, pass `--session <id>`.

Then tell the user, briefly:

- The URL, and what's in the review: files, items, how many high-importance items, and anything you flagged in `concerns` that deserves attention first.
- How the page works: files on the left (filterable by High/Medium/Low importance), explained changes inline with **Confirm** and **Request Change** (it asks for a reason and is saved as `declined`), connected files on the right including unchanged context, and keys `j`/`k`/`c`/`d`/`n`. For speed, each file has **Confirm all in file** (`Shift+C`) and the left bar has **Confirm all Low/Medium Importance**; bulk actions only touch pending items, never high-importance ones from the sidebar, and can be undone from the toast.
- **Ask Claude** on any change (key `a`), or the box on the overview for branch-wide questions. Each change keeps its own thread with follow-ups. The answering session can read and search the code but can't edit anything or run commands. Answers take a few seconds and count toward their Claude usage.
- Every click is saved to `<review-dir>/review.json` right away. When they're done, click **Finish review** (or just tell you).

Then stop and wait. Don't start fixing anything while the review is in progress.

## 5. After the review

When the user clicks **Finish review**, the server exits and you're notified that the background command completed. If the user says they're done instead, stop the server. Either way, read `<review-dir>/review.json`:

```json
{
  "status": "finished",
  "base": "main", "head": "feature/cursor-pagination", "merge_base": "…",
  "counts": { "total": 14, "confirmed": 11, "declined": 2, "pending": 1, "bulk_confirmed": 6 },
  "items": [
    { "change_id": "c1", "file": "src/api/users.py", "side": "new", "start": 42, "end": 67,
      "title": "Paginate GET /users with cursor tokens", "importance": "high",
      "status": "declined", "reason": "Cap limit at 100 and return 400 for ?page=", "decided_at": "…", "bulk": false }
  ]
}
```

`bulk: true` means the item was confirmed with "Confirm all in file" or "Confirm all Low/Medium Importance" rather than one by one, so the user may not have read it closely.

`questions` holds the threads from the page: `{ "thread": "c1" | "general", "change_id", "file", "start", "end", "title", "session_id", "messages": [{ "question", "answer", "asked_at" }] }`. You didn't see these conversations, so read them. A question often points at something unclear or wrong, and an answer may have promised a fix ("changes are made after the review").

Report back in a few lines: counts (say how many confirmations were bulk), then each declined item as `file:start-end — title` followed by the user's reason, then any items left pending, then any question whose answer exposed a problem the user didn't turn into a change request. Then **offer** to address the declined items. Propose a concrete fix per item in one line each and ask before editing. The reasons are the user's instructions; if one is ambiguous, ask instead of guessing. After fixing, offer to run `/i-read-code` again so they can review the follow-up changes.

## Notes

- The page loads syntax highlighting from cdnjs. Offline, it falls back to plain text and still works.
- `review.json` is the record of the review. The rest of `<review-dir>` (`changes.json`, `diff.txt`, `data.json`, `index.html`) is regenerated on every run.
- Each question thread is a saved Claude Code session (it shows up in `claude --resume`). The first question in a thread forks this session; follow-ups resume that fork. Resuming a review in a new session forks the new one; old threads keep their own forks.
- If port 8765 is busy, the server picks the next free one. If the browser didn't open, give the user the printed URL.
