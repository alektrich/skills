#!/usr/bin/env python3
"""End-to-end tests for the i-read-code scripts.

Each test builds a throwaway git repo, runs the real scripts against it and
checks what they write. Standard library only:

  python3 -m unittest discover -s skills/i-read-code/tests
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
GAP_TITLE = "Change without an explanation"
SNIPPET = "import os\n\nprint(os.name)\n"


def run(*args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, env=env)


def make_repo(root):
    """A repo whose feature branch adds a.py and b.py; each has a blank line 2."""
    run("git", "init", "-q", "-b", "main", cwd=root)
    run("git", "config", "user.email", "test@example.com", cwd=root)
    run("git", "config", "user.name", "Test", cwd=root)
    with open(os.path.join(root, "README.md"), "w") as fh:
        fh.write("base\n")
    run("git", "add", "-A", cwd=root)
    run("git", "commit", "-qm", "base", cwd=root)
    run("git", "checkout", "-qb", "feature", cwd=root)
    for name in ("a.py", "b.py"):
        with open(os.path.join(root, name), "w") as fh:
            fh.write(SNIPPET)
    run("git", "add", "-A", cwd=root)
    run("git", "commit", "-qm", "add a and b", cwd=root)


ANALYSIS = {
    "overview": "Adds two scripts.",
    "changes": [
        # Whole-file item: must count as covering every line of a.py.
        {"id": "a", "file": "a.py", "side": "file", "title": "New a.py", "explanation": "Prints the OS name."},
        # Line items that skip blank line 2: the blank line must not need its own item.
        {"id": "b1", "file": "b.py", "side": "new", "start": 1, "end": 1, "title": "Import", "explanation": "Imports os."},
        {"id": "b3", "file": "b.py", "side": "new", "start": 3, "end": 3, "title": "Print", "explanation": "Prints the OS name."},
    ],
}


# Mimics `claude -p --output-format stream-json`: a fork gets a new session id, a resume keeps it.
FAKE_CLAUDE = r'''
import json, os, sys
args, prompt = sys.argv[1:], sys.stdin.read()
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as fh:
    fh.write(json.dumps({"args": args, "prompt": prompt}) + "\n")
if "fail please" in prompt:
    sys.exit("boom")
resume = args[args.index("--resume") + 1] if "--resume" in args else None
sid = "fork-1" if "--fork-session" in args else resume or "fresh-1"
# Save the session like claude does, so cleanup has something to delete.
config = os.environ["CLAUDE_CONFIG_DIR"]
os.makedirs(os.path.join(config, "projects", "-repo"), exist_ok=True)
os.makedirs(os.path.join(config, "session-env", sid), exist_ok=True)
open(os.path.join(config, "projects", "-repo", sid + ".jsonl"), "a").close()
def out(event):
    print(json.dumps(event), flush=True)
out({"type": "system", "subtype": "init", "session_id": sid})
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/repo/b.py"}}]}})
for part in ("It ", "imports os."):
    out({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": part}}})
out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": "It imports os."})
'''


# Mimics `cursor-agent -p --output-format stream-json --stream-partial-output`: the prompt is the last
# argument, a new chat gets a new id, --resume keeps it. Assistant events follow Cursor's three kinds.
FAKE_CURSOR = r'''
import json, os, sys
args = sys.argv[1:]
if args == ["--help"]:
    sys.exit(print("  --trust  Trust the workspace without prompting"))
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as fh:
    fh.write(json.dumps({"args": args[:-1], "prompt": args[-1]}) + "\n")
sid = args[args.index("--resume") + 1] if "--resume" in args else "chat-1"
os.makedirs(os.path.join(os.environ["CURSOR_CONFIG_DIR"], "chats", "ws", sid), exist_ok=True)
os.makedirs(os.path.join(os.environ["CURSOR_CONFIG_DIR"], "projects", "repo", "agent-transcripts", sid), exist_ok=True)
def out(event):
    print(json.dumps(dict(event, session_id=sid)), flush=True)
def said(text, **extra):
    out(dict({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}, **extra))
out({"type": "system", "subtype": "init", "model": "fake"})
said("Let me look.", timestamp_ms=1)
said("Let me look.", timestamp_ms=2, model_call_id="m1")  # buffered flush before the tool call
out({"type": "tool_call", "subtype": "started", "call_id": "t1", "tool_call": {"readToolCall": {"args": {"path": "b.py"}},
     "hookAdditionalContexts": [], "toolCallId": "t1", "startedAtMs": "1"}})
said("It imports ", timestamp_ms=3)
said("os.", timestamp_ms=4)
said("It imports os.")  # final flush
out({"type": "result", "subtype": "success", "is_error": False, "result": "Let me look.It imports os."})
'''


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.out = os.path.join(self.tmp.name, "review")
        os.makedirs(self.repo)
        make_repo(self.repo)
        run(sys.executable, os.path.join(SCRIPTS, "collect_changes.py"), "--out", self.out, cwd=self.repo)
        with open(os.path.join(self.out, "analysis.json"), "w") as fh:
            json.dump(ANALYSIS, fh)
        run(sys.executable, os.path.join(SCRIPTS, "build_review.py"), "--dir", self.out, cwd=self.repo)
        with open(os.path.join(self.out, "data.json")) as fh:
            self.data = json.load(fh)

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_finds_branch_changes_against_main(self):
        self.assertEqual(self.data["meta"]["base"], "main")
        self.assertEqual({f["path"] for f in self.data["files"]}, {"a.py", "b.py"})

    def test_explained_lines_get_no_gap_items(self):
        gaps = [(c["file"], c["start"], c["end"]) for c in self.data["changes"] if c["title"] == GAP_TITLE]
        self.assertEqual(gaps, [], "whole-file items and skipped blank lines must not produce gap items")
        self.assertEqual({c["id"] for c in self.data["changes"]}, {"a", "b1", "b3"})

    def test_unexplained_code_line_gets_a_gap_item(self):
        analysis = dict(ANALYSIS, changes=[c for c in ANALYSIS["changes"] if c["id"] != "b3"])
        with open(os.path.join(self.out, "analysis.json"), "w") as fh:
            json.dump(analysis, fh)
        run(sys.executable, os.path.join(SCRIPTS, "build_review.py"), "--dir", self.out, cwd=self.repo)
        with open(os.path.join(self.out, "data.json")) as fh:
            data = json.load(fh)
        gaps = [(c["file"], c["start"], c["end"]) for c in data["changes"] if c["title"] == GAP_TITLE]
        self.assertEqual(gaps, [("b.py", 3, 3)])

    def serve(self, *extra, env=None):
        server = subprocess.Popen(
            [sys.executable, os.path.join(SCRIPTS, "serve.py"), "--dir", self.out, "--no-open", *extra],
            stdout=subprocess.PIPE, text=True, env=env,
        )
        url = server.stdout.readline().split("Review page: ")[1].strip()
        self.url = url

        def post(path, body):
            req = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=5))

        return server, post

    def ask(self, body):
        """POST api/ask and return the streamed events."""
        req = urllib.request.Request(self.url + "api/ask", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as res:
            return [json.loads(line) for line in res if line.strip()]

    def state(self):
        return json.load(urllib.request.urlopen(self.url + "api/state", timeout=5))

    def fake_claude(self):
        """A stand-in for the claude CLI that logs each call and streams a canned answer."""
        path = os.path.join(self.tmp.name, "claude")
        with open(path, "w") as fh:
            fh.write(f"#!{sys.executable}\n" + FAKE_CLAUDE)
        os.chmod(path, 0o755)
        self.claude_log = os.path.join(self.tmp.name, "claude-calls.jsonl")
        self.config = os.path.join(self.tmp.name, "claude-config")
        self.main_transcript = os.path.join(self.config, "projects", "-repo", "main-1.jsonl")
        os.makedirs(os.path.dirname(self.main_transcript))
        open(self.main_transcript, "w").close()
        return path

    def fake_cursor(self):
        path = os.path.join(self.tmp.name, "cursor-agent")
        with open(path, "w") as fh:
            fh.write(f"#!{sys.executable}\n" + FAKE_CURSOR)
        os.chmod(path, 0o755)
        self.claude_log = os.path.join(self.tmp.name, "cursor-calls.jsonl")
        self.cursor_config = os.path.join(self.tmp.name, "cursor-config")
        return path

    def test_cursor_answers_in_new_chats_that_get_the_overview(self):
        cursor = self.fake_cursor()
        env = dict(os.environ, FAKE_CLAUDE_LOG=self.claude_log, CURSOR_CONFIG_DIR=self.cursor_config,
                   CURSOR_AGENT="1", CLAUDE_CODE_SESSION_ID="", AI_AGENT="")
        server, post = self.serve("--cli", cursor, env=env)  # detected from CURSOR_AGENT
        try:
            self.assertEqual(self.state()["ask"], {"available": True, "forked": False, "agent": "Cursor"})
            events = self.ask({"change_id": "b1", "question": "Why import os?"})
            self.assertEqual([e.get("text") or e.get("status") for e in events[:-1]],
                             ["Let me look.", "Reading b.py", "\n\n", "It imports ", "os."])
            self.assertEqual(events[-1]["thread"]["messages"][0]["answer"], "Let me look.\n\nIt imports os.")
            self.ask({"change_id": "b1", "question": "And later?"})
            self.assertEqual(os.listdir(os.path.join(self.cursor_config, "chats", "ws")), ["chat-1"])
            post("api/finish", {})
            server.wait(timeout=5)
        finally:
            self.stop(server)
        first, follow = self.claude_calls()
        self.assertEqual(first["args"][first["args"].index("--mode") + 1], "ask")
        self.assertNotIn("--resume", first["args"])
        self.assertNotIn("--force", first["args"])
        self.assertIn("--trust", first["args"], "a CLI whose --help lists --trust gets it")
        self.assertIn("Adds two scripts.", first["prompt"], "a new chat gets the overview")
        self.assertIn("do not edit files", first["prompt"])
        self.assertEqual(follow["args"][follow["args"].index("--resume") + 1], "chat-1")
        with open(os.path.join(self.out, "review.json")) as fh:
            review = json.load(fh)
        self.assertEqual((review["agent"], review["session_id"]), ("cursor", None))
        self.assertEqual(os.listdir(os.path.join(self.cursor_config, "chats", "ws")), [], "Finish deletes the chat")
        self.assertEqual(os.listdir(os.path.join(self.cursor_config, "projects", "repo", "agent-transcripts")), [])

    def saved_sessions(self):
        """Session ids with a transcript in the fake claude config dir."""
        return sorted(n[:-6] for n in os.listdir(os.path.join(self.config, "projects", "-repo")))

    def claude_calls(self):
        with open(self.claude_log) as fh:
            return [json.loads(line) for line in fh]

    def test_questions_fork_the_main_session_then_resume_their_thread(self):
        claude = self.fake_claude()
        env = dict(os.environ, CLAUDE_CODE_SESSION_ID="main-1", FAKE_CLAUDE_LOG=self.claude_log,
                   CLAUDE_CONFIG_DIR=self.config)
        server, _ = self.serve("--agent", "claude", "--cli", claude, env=env)
        try:
            self.assertEqual(self.state()["ask"], {"available": True, "forked": True, "agent": "Claude Code"})
            events = self.ask({"change_id": "b1", "question": "Why import os?"})
            self.assertEqual([e["type"] for e in events], ["status", "text", "text", "done"])
            self.assertEqual(events[0]["status"], "Reading b.py")
            self.assertEqual(events[-1]["thread"]["messages"][0]["answer"], "It imports os.")
            self.ask({"change_id": "b1", "question": "And later?"})
            self.ask({"change_id": None, "question": "What does the branch do?"})
            failed = self.ask({"change_id": "b3", "question": "fail please"})
            self.assertEqual(failed[-1], {"type": "error", "error": "boom"})
            for bad in ({"change_id": "b1", "question": "  "}, {"change_id": "nope", "question": "Why?"}):
                with self.assertRaises(urllib.error.HTTPError) as err:
                    self.ask(bad)
                self.assertEqual(err.exception.code, 400)
                err.exception.close()
        finally:
            self.stop(server)

        first, follow, general, _ = self.claude_calls()
        # The first question in a thread forks the main session and carries the item's context.
        self.assertIn("--fork-session", first["args"])
        self.assertEqual(first["args"][first["args"].index("--resume") + 1], "main-1")
        self.assertEqual(first["args"][first["args"].index("--tools") + 1:][:3], ["Read", "Grep", "Glob"])
        self.assertIn("b.py", first["prompt"])
        self.assertIn("Imports os.", first["prompt"])
        # A follow-up resumes that thread's fork.
        self.assertNotIn("--fork-session", follow["args"])
        self.assertEqual(follow["args"][follow["args"].index("--resume") + 1], "fork-1")
        self.assertTrue(follow["prompt"].startswith("[Follow-up"))
        self.assertIn("--fork-session", general["args"])

        with open(os.path.join(self.out, "review.json")) as fh:
            review = json.load(fh)
        self.assertEqual(review["session_id"], "main-1")
        threads = {t["thread"]: t for t in review["questions"]}
        self.assertEqual(set(threads), {"b1", "general"}, "a failed answer must not be saved")
        self.assertEqual((threads["b1"]["file"], threads["b1"]["session_id"]), ("b.py", "fork-1"))
        self.assertEqual([m["question"] for m in threads["b1"]["messages"]], ["Why import os?", "And later?"])
        self.assertEqual(review["agent_sessions"], ["fork-1"])
        self.assertEqual(self.saved_sessions(), ["fork-1", "main-1"])

        # A restarted server, even one without the env var, keeps the session and the threads.
        env.pop("CLAUDE_CODE_SESSION_ID")
        server, _ = self.serve("--agent", "claude", "--cli", claude, env=env)
        try:
            state = self.state()
            self.assertEqual(state["session_id"], "main-1")
            self.assertEqual(len(state["questions"]), 2)
        finally:
            self.stop(server)

        # Stopping the server leaves the forks; --finish deletes them but never the main session.
        run(sys.executable, os.path.join(SCRIPTS, "serve.py"), "--dir", self.out, "--finish", cwd=self.repo, env=env)
        self.assertEqual(self.saved_sessions(), ["main-1"])
        self.assertFalse(os.path.exists(os.path.join(self.config, "session-env", "fork-1")))
        with open(os.path.join(self.out, "review.json")) as fh:
            review = json.load(fh)
        self.assertEqual((review["status"], review["agent_sessions"]), ("finished", []))
        self.assertEqual({t["session_id"] for t in review["questions"]}, {None})

        # Asking again in the reopened review forks anew and replays the thread so far.
        server, post = self.serve("--agent", "claude", "--cli", claude, env=env)
        try:
            self.ask({"change_id": "b1", "question": "One more?"})
            post("api/finish", {})
            server.wait(timeout=5)
        finally:
            self.stop(server)
        again = self.claude_calls()[-1]
        self.assertIn("--fork-session", again["args"])
        self.assertIn("Q: And later?", again["prompt"])
        self.assertEqual(self.saved_sessions(), ["main-1"], "Finish review deletes the question sessions")

    def test_questions_are_off_without_the_claude_cli(self):
        server, _ = self.serve("--agent", "claude", "--cli", os.path.join(self.tmp.name, "no-such-claude"))
        try:
            self.assertFalse(self.state()["ask"]["available"])
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.ask({"change_id": "a", "question": "Why?"})
            self.assertEqual(err.exception.code, 503)
            err.exception.close()
        finally:
            self.stop(server)

    def stop(self, server):
        if server.poll() is None:
            server.kill()
        server.wait()
        server.stdout.close()

    def test_bulk_approve_and_undo(self):
        server, post = self.serve()
        try:
            post("api/decision", {"change_id": "a", "status": "approved"})
            doc = post("api/decisions", {"change_ids": ["b1", "b3"], "status": "approved"})
            self.assertEqual((doc["counts"]["approved"], doc["counts"]["bulk_approved"]), (3, 2))
            doc = post("api/decisions", {"change_ids": ["b3"], "status": "pending"})
            items = {i["change_id"]: i for i in doc["items"]}
            self.assertEqual((items["b1"]["status"], items["b3"]["status"]), ("approved", "pending"))
            self.assertEqual({k: v["bulk"] for k, v in items.items()}, {"a": False, "b1": True, "b3": False})
            # Re-approving one by one clears the flag.
            doc = post("api/decision", {"change_id": "b1", "status": "approved"})
            self.assertEqual(doc["counts"]["bulk_approved"], 0)
            for bad in ({"change_ids": ["a"], "status": "declined"}, {"change_ids": ["nope"], "status": "approved"},
                        {"change_ids": [], "status": "approved"}):
                with self.assertRaises(urllib.error.HTTPError) as err:
                    post("api/decisions", bad)
                self.assertEqual(err.exception.code, 400)
                err.exception.close()
        finally:
            self.stop(server)
        with open(os.path.join(self.out, "review.json")) as fh:
            self.assertEqual(json.load(fh)["counts"]["approved"], 2)

    def test_agent_detection(self):
        sys.path.insert(0, SCRIPTS)
        import agents
        none = lambda cli: None
        self.assertEqual(agents.detect({"CLAUDE_CODE_SESSION_ID": "x"}, none).name, "claude")
        self.assertEqual(agents.detect({"AI_AGENT": "claude-code_2_agent"}, none).name, "claude")
        self.assertEqual(agents.detect({"CURSOR_AGENT": "1"}, none).name, "cursor")
        # Cursor started from Claude Code inherits Claude's variables; CURSOR_AGENT still wins.
        self.assertEqual(agents.detect({"CURSOR_AGENT": "1", "AI_AGENT": "claude-code_2_agent",
                                        "CLAUDE_CODE_SESSION_ID": "x"}, none).name, "cursor")
        both = lambda cli: "/bin/" + cli
        self.assertEqual(agents.detect({}, both).name, "claude")
        only_cursor = lambda cli: "/bin/agent" if cli == "cursor-agent" else None
        self.assertEqual(agents.detect({}, only_cursor).name, "cursor")
        self.assertEqual(agents.detect({}, none).name, "claude")
        self.assertEqual(agents.delete("cursor", ["../../etc"]), 0, "ids can't escape the config dir")

    def test_only_the_last_three_reviews_are_kept(self):
        collect = os.path.join(SCRIPTS, "collect_changes.py")
        reviews = os.path.join(self.repo, ".i-read-code")
        os.makedirs(os.path.join(reviews, "screenshots"))  # not a review; must survive
        for i, branch in enumerate(("f1", "f2", "f3", "f4")):
            run("git", "checkout", "-qb", branch, cwd=self.repo)
            out = run(sys.executable, collect, cwd=self.repo).stdout
            os.utime(os.path.join(reviews, branch, "changes.json"), (1000 + i, 1000 + i))
        self.assertIn("Removed 1 older review(s) to keep the last 3: f1", out)
        self.assertEqual(sorted(os.listdir(reviews)), ["f2", "f3", "f4", "screenshots"])

    def test_rebuilds_keep_two_archived_reviews(self):
        hist = os.path.join(self.out, "history")
        for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
            os.makedirs(hist, exist_ok=True)
            with open(os.path.join(hist, f"review-{stamp}.json"), "w") as fh:
                json.dump({}, fh)
        with open(os.path.join(self.out, "review.json"), "w") as fh:
            json.dump({"status": "in_progress"}, fh)
        run(sys.executable, os.path.join(SCRIPTS, "build_review.py"), "--dir", self.out, cwd=self.repo)
        kept = sorted(os.listdir(hist))
        self.assertEqual(len(kept), 2)
        self.assertEqual(kept[0], "review-20260103-000000.json")

    def test_approve_and_decline_are_saved_to_review_json(self):
        server, post = self.serve()
        try:
            post("api/decision", {"change_id": "a", "status": "approved"})
            post("api/decision", {"change_id": "b1", "status": "declined", "reason": "Import inside main()"})
            with self.assertRaises(urllib.error.HTTPError) as err:
                post("api/decision", {"change_id": "b3", "status": "declined", "reason": "  "})
            self.assertEqual(err.exception.code, 400)
            err.exception.close()
            post("api/finish", {})
            server.wait(timeout=5)
        finally:
            self.stop(server)

        with open(os.path.join(self.out, "review.json")) as fh:
            review = json.load(fh)
        items = {i["change_id"]: i for i in review["items"]}
        self.assertEqual(review["status"], "finished")
        self.assertEqual(review["counts"], {"total": 3, "approved": 1, "declined": 1, "pending": 1, "bulk_approved": 0})
        self.assertEqual(items["a"]["status"], "approved")
        declined = items["b1"]
        self.assertEqual((declined["status"], declined["reason"]), ("declined", "Import inside main()"))
        self.assertEqual((declined["file"], declined["start"], declined["end"]), ("b.py", 1, 1))
        self.assertEqual(items["b3"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
