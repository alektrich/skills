"""The coding agents that can answer "Ask Agent" questions, and cleanup of their sessions.

Everything that differs between agents lives here: how to find the CLI, the
command for one read-only question, how to read its stream-json output, and
where it saves sessions. serve.py runs the process and doesn't know which
agent it is talking to.

  claude   `claude -p`; forks the session that wrote the review, so answers know the analysis
  cursor   `cursor-agent -p --mode ask`; can't fork, so a thread starts a new chat and the
           prompt carries the analysis instead

Sessions a question leaves behind are only useful while the review is open,
because follow-ups resume them. The Q&A itself is kept in review.json, so once
the review is finished or thrown away the sessions are deleted. The session
that wrote the review is never touched. Standard library only.
"""

import glob
import os
import re
import shutil
import subprocess

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")  # keeps ids from escaping the config dir
INSTRUCTIONS = (
    "You are answering a question the user typed into the i-read-code review page while they "
    "review this branch. The review is still in progress: do not edit files, run commands, restart "
    "the server or record decisions; only read and search the code. Answer the question directly "
    "and concisely in Markdown, citing file:line where it helps. If the question exposes a real "
    "problem, say so plainly and describe the fix; changes are made after the review."
)


def remove(paths):
    """Delete files and folders; True if anything was there."""
    found = False
    for p in paths:
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            elif os.path.lexists(p):
                os.remove(p)
            else:
                continue
            found = True
        except OSError:
            pass
    return found


class Claude:
    name = "claude"
    label = "Claude Code"
    clis = ("claude",)
    can_fork = True
    answer_from_stream = False
    tools = ["Read", "Grep", "Glob"]
    per_session_dirs = ("session-env", "file-history", "todos", "tasks")

    @staticmethod
    def started_us(env):
        return bool(env.get("CLAUDE_CODE_SESSION_ID")) or env.get("AI_AGENT", "").startswith("claude")

    @staticmethod
    def own_session(env):
        return env.get("CLAUDE_CODE_SESSION_ID")

    def command(self, cli, prompt, resume, fork, cwd):
        """Returns (argv, stdin). The prompt goes in on stdin so it can't be mistaken for an option."""
        cmd = [cli, "-p", "--output-format", "stream-json", "--include-partial-messages", "--verbose",
               "--tools", *self.tools, "--allowedTools", *self.tools, "--permission-mode", "dontAsk",
               "--strict-mcp-config", "--append-system-prompt", INSTRUCTIONS]
        if resume:
            cmd += ["--resume", resume] + (["--fork-session"] if fork else [])
        return cmd, prompt

    def parse(self, ev, run):
        """Yields ("session", id), ("text", delta) and ("status", line) for one stream-json event."""
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            yield "session", ev.get("session_id")
        elif kind == "stream_event":
            e = ev.get("event") or {}
            if e.get("type") == "content_block_start" and (e.get("content_block") or {}).get("type") == "text":
                if run.get("streamed"):
                    yield "text", "\n\n"
            elif e.get("type") == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
                run["streamed"] = True
                yield "text", e["delta"]["text"]
        elif kind == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    yield "status", tool_status(block.get("name", ""), block.get("input") or {})

    def config_dir(self):
        return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")

    def session_paths(self, sid):
        root = self.config_dir()
        paths = glob.glob(os.path.join(root, "projects", "*", sid + ".jsonl"))
        paths += glob.glob(os.path.join(root, "projects", "*", sid))
        paths += [os.path.join(root, d, sid) for d in self.per_session_dirs]
        paths += glob.glob(os.path.join(root, "todos", sid + "-*.json"))
        return paths


class Cursor:
    name = "cursor"
    label = "Cursor"
    clis = ("cursor-agent", "agent")
    can_fork = False
    answer_from_stream = True  # its result joins text blocks with no space ("...inputs.No, ..."), the stream doesn't

    def __init__(self):
        self.help = {}

    def supports(self, cli, flag):
        """Older CLIs reject options they don't know (e.g. --trust before mid-2026), so check --help once."""
        if cli not in self.help:
            try:
                self.help[cli] = subprocess.run([cli, "--help"], capture_output=True, text=True, timeout=20).stdout
            except (OSError, subprocess.SubprocessError):
                self.help[cli] = ""
        return flag in self.help[cli]

    @staticmethod
    def started_us(env):
        return bool(env.get("CURSOR_AGENT")) or "cursor" in env.get("AI_AGENT", "").lower()

    @staticmethod
    def own_session(env):
        return None  # Cursor chats can't be forked, so the reviewing chat is never needed

    def command(self, cli, prompt, resume, fork, cwd):
        # Ask mode is Cursor's read-only mode; without --force no shell command gets approved.
        # There's no system prompt option, so a new chat gets the instructions in the prompt.
        cmd = [cli, "-p", "--output-format", "stream-json", "--stream-partial-output", "--mode", "ask",
               "--workspace", cwd]
        if self.supports(cli, "--trust"):
            cmd.append("--trust")  # headless runs otherwise stop at the workspace trust prompt
        if resume:
            cmd += ["--resume", resume]
        else:
            prompt = f"{INSTRUCTIONS}\n\n{prompt}"
        return cmd + [prompt], None

    def parse(self, ev, run):
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            yield "session", ev.get("session_id")
        elif kind == "assistant":
            # With --stream-partial-output only events with timestamp_ms and no model_call_id
            # carry new text; the others repeat text already streamed.
            if "timestamp_ms" in ev and "model_call_id" not in ev:
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "text" and block.get("text"):
                        if run.pop("after_tool", False) and run.get("streamed"):
                            yield "text", "\n\n"
                        run["streamed"] = True
                        yield "text", block["text"]
        elif kind == "tool_call" and ev.get("subtype") == "started":
            run["after_tool"] = True
            # The tool is the one "<name>ToolCall" key; the others are metadata (toolCallId, startedAtMs, ...).
            for key, call in (ev.get("tool_call") or {}).items():
                if key.endswith("ToolCall") and isinstance(call, dict):
                    name = key[:-len("ToolCall")]
                    yield "status", tool_status(name[:1].upper() + name[1:], call.get("args") or {})

    def session_paths(self, sid):
        # chats/<workspace hash>/<id>/ holds the chat; newer CLIs also keep a transcript per workspace.
        root = os.environ.get("CURSOR_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".cursor")
        return (glob.glob(os.path.join(root, "chats", "*", sid))
                + glob.glob(os.path.join(root, "projects", "*", "agent-transcripts", sid)))


# Cursor first: its shell sets CURSOR_AGENT but inherits AI_AGENT / CLAUDE_CODE_SESSION_ID when it was
# started from Claude Code, while Claude Code never sets CURSOR_AGENT.
AGENTS = {a.name: a for a in (Cursor(), Claude())}


def tool_status(name, args):
    """A short line for the page while the answering session uses a tool."""
    path = args.get("file_path") or args.get("path") or args.get("target_file") or ""
    pattern = str(args.get("pattern") or args.get("globPattern") or args.get("query") or "")[:60]
    if name == "Read":
        return f"Reading {os.path.basename(path) or 'a file'}"
    if name == "Grep":
        return f"Searching for {pattern}"
    if name == "Glob":
        return f"Looking for files matching {pattern}"
    if name in ("SemSearch", "CodebaseSearch") and pattern:
        return f"Searching the codebase for {pattern}"
    if name == "Ls":
        return f"Listing {os.path.basename(path.rstrip('/')) or 'files'}"
    return f"Using {name}"


def detect(env, which=shutil.which):
    """The agent this server should use: the one that started it, else the first CLI installed."""
    for agent in AGENTS.values():
        if agent.started_us(env):
            return agent
    for agent in (AGENTS["claude"], AGENTS["cursor"]):
        if any(which(c) for c in agent.clis):
            return agent
    return AGENTS["claude"]  # questions are then off, and the page says the CLI wasn't found


def find_cli(agent, override=None, which=shutil.which):
    for c in [override] if override else agent.clis:
        path = which(c)
        if path:
            return path
    return None


def spawned(doc):
    """Session ids a review.json document says the review server created (not the one that wrote it)."""
    ids = set(doc.get("agent_sessions") or [])
    ids.update(t.get("session_id") for t in doc.get("questions") or [])
    ids.discard(None)
    ids.discard(doc.get("session_id"))
    return ids


def delete(agent_name, ids, keep=None):
    """Remove every file the agent stored for these sessions; returns how many sessions had files."""
    agent = AGENTS.get(agent_name or "claude")
    if not agent:
        return 0
    return sum(remove(agent.session_paths(sid)) for sid in ids
               if sid and sid != keep and SESSION_ID_RE.match(sid))


def delete_for(doc):
    """Delete the sessions recorded in a review.json document (written before `agent` existed = claude)."""
    return delete(doc.get("agent"), spawned(doc), keep=doc.get("session_id"))
