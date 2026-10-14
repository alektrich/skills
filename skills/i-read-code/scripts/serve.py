#!/usr/bin/env python3
"""Serve the review page and save every decision to <dir>/review.json.

  python3 serve.py --dir <review-dir> [--port N] [--no-open] [--agent claude|cursor] [--cli CMD] [--session ID]
  python3 serve.py --dir <review-dir> --finish

Listens on 127.0.0.1 only. Each Approve/Decline (and each bulk approve) is written to review.json
immediately (atomic replace), so closing the browser loses nothing; restarting
the server resumes from the saved file. The process exits when the reviewer
clicks "Finish review", which lets whoever started it in the background know
the review is done. Standard library only.

Questions typed into the page are answered by the coding agent that started
the server (Claude Code or Cursor, see agents.py), read-only. Claude forks the
session that wrote the review (CLAUDE_CODE_SESSION_ID, which a server started
from that session inherits), so the answer knows the analysis; Cursor starts a
new chat whose prompt carries the analysis. Each change gets its own thread;
follow-ups resume it. Threads are saved to review.json, and every session the
server starts is listed under agent_sessions. Finishing the review deletes
those sessions; --finish does the same for a server that was stopped instead
of finished from the page.
"""

import argparse
import datetime
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agents

STATUSES = ("pending", "approved", "declined")
GENERAL = "general"  # thread id for questions about the branch as a whole
ASK_TIMEOUT = 600  # seconds before a stuck agent process is killed


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class Review:
    def __init__(self, directory, agent, session_id=None):
        self.dir = directory
        self.path = os.path.join(directory, "review.json")
        self.data = json.load(open(os.path.join(directory, "data.json")))
        self.changes = {c["id"]: c for c in self.data["changes"]}
        self.files = {f["path"]: f for f in self.data["files"]}
        self.lock = threading.Lock()
        saved, prev = {}, {}
        if os.path.exists(self.path):
            try:
                prev = json.load(open(self.path))
                if prev.get("fingerprint") == self.data["meta"].get("fingerprint"):
                    saved = {r["change_id"]: r for r in prev.get("items", [])}
                else:
                    prev = {}
            except (OSError, ValueError):
                prev = {}
        self.status = "in_progress"
        self.items = {}
        for cid, c in self.changes.items():
            r = saved.get(cid) or {}
            status = r.get("status", "pending")
            if status == "confirmed":  # review.json written before Confirm became Approve
                status = "approved"
            self.items[cid] = {"status": status, "reason": r.get("reason", ""),
                               "decided_at": r.get("decided_at"), "bulk": bool(r.get("bulk"))}
        self.agent = agent
        if prev and prev.get("agent", "claude") != agent.name:
            # Another agent's sessions can't be resumed: delete them; threads replay their Q&A instead.
            agents.delete_for(prev)
            prev = dict(prev, session_id=None, agent_sessions=[],
                        questions=[dict(t, session_id=None) for t in prev.get("questions", [])])
        # The session that wrote the analysis; questions fork from it when the agent can fork.
        self.session_id = session_id or prev.get("session_id")
        self.threads = {}
        for t in prev.get("questions", []):
            tid = t.get("thread")
            if tid == GENERAL or tid in self.changes:
                self.threads[tid] = {"session_id": t.get("session_id"), "messages": t.get("messages", [])}
        # Every session a question started, answered or not, so finishing can delete them all.
        self.spawned = list(prev.get("agent_sessions") or [])
        self.busy = set()  # thread ids with an agent process running
        self.write()

    def record(self, cid, change):
        old = self.files.get(change["file"], {}).get("old_path")
        rec = {
            "change_id": cid, "file": change["file"], "side": change["side"],
            "start": change["start"], "end": change["end"],
            "title": change["title"], "importance": change["importance"],
            "status": self.items[cid]["status"],
            "reason": self.items[cid]["reason"] if self.items[cid]["status"] == "declined" else "",
            "decided_at": self.items[cid]["decided_at"],
            # True when approved via "Approve all in file" / "Approve all <level>", not read one by one.
            "bulk": self.items[cid]["status"] == "approved" and self.items[cid].get("bulk", False),
        }
        if old:
            rec["old_path"] = old
        return rec

    def document(self):
        m = self.data["meta"]
        items = [self.record(cid, c) for cid, c in self.changes.items()]
        counts = {s: sum(1 for i in items if i["status"] == s) for s in STATUSES}
        counts["bulk_approved"] = sum(1 for i in items if i["bulk"])
        counts["total"] = len(items)
        return {
            "tool": "i-read-code", "version": 1, "status": self.status, "updated_at": now(),
            "repo": m["repo"], "base": m["base"], "base_sha": m["base_sha"], "merge_base": m["merge_base"],
            "head": m["head"], "head_sha": m["head_sha"], "include_uncommitted": m["include_uncommitted"],
            "fingerprint": m.get("fingerprint"), "counts": counts, "items": items,
            "agent": self.agent.name, "session_id": self.session_id,
            "questions": [self.thread_record(t) for t in self.threads], "agent_sessions": self.spawned,
        }

    def thread_record(self, tid):
        t = self.threads[tid]
        rec = {"thread": tid, "session_id": t["session_id"], "messages": t["messages"]}
        if tid != GENERAL:
            c = self.changes[tid]
            rec.update(change_id=tid, file=c["file"], start=c["start"], end=c["end"], title=c["title"])
        return rec

    def write(self):
        doc = self.document()
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(doc, fh, indent=2)
        os.replace(tmp, self.path)
        return doc

    def decide(self, cid, status, reason):
        if cid not in self.changes:
            raise ValueError(f"unknown change id {cid!r}")
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        reason = (reason or "").strip()
        if status == "declined" and not reason:
            raise ValueError("a declined change needs a reason")
        with self.lock:
            self.items[cid] = {"status": status, "reason": reason if status == "declined" else "",
                               "decided_at": now() if status != "pending" else None, "bulk": False}
            self.status = "in_progress"
            return self.write()

    def decide_many(self, cids, status):
        """Bulk approve (or reset to pending) in one write; declining always needs a per-item reason."""
        if status not in ("approved", "pending"):
            raise ValueError("bulk decisions can only approve or reset to pending")
        if not isinstance(cids, list) or not cids:
            raise ValueError("change_ids must be a non-empty list")
        unknown = [c for c in cids if c not in self.changes]
        if unknown:
            raise ValueError(f"unknown change ids {unknown!r}")
        with self.lock:
            stamp = now() if status != "pending" else None
            for cid in cids:
                self.items[cid] = {"status": status, "reason": "", "decided_at": stamp,
                                   "bulk": status == "approved"}
            self.status = "in_progress"
            return self.write()

    def finish(self):
        """Mark the review finished and delete the question sessions; the Q&A stays in review.json."""
        with self.lock:
            self.status = "finished"
            removed = agents.delete_for(self.document())
            self.spawned = []
            for t in self.threads.values():
                t["session_id"] = None
            return self.write(), removed

    def track(self, session_id):
        with self.lock:
            if session_id and session_id != self.session_id and session_id not in self.spawned:
                self.spawned.append(session_id)
                self.write()

    def claim_thread(self, tid):
        """Mark a thread busy so one question at a time runs per thread; returns its fork id or None."""
        if tid != GENERAL and tid not in self.changes:
            raise ValueError(f"unknown change id {tid!r}")
        with self.lock:
            if tid in self.busy:
                return False, None
            self.busy.add(tid)
            return True, self.threads.get(tid, {}).get("session_id")

    def release_thread(self, tid):
        with self.lock:
            self.busy.discard(tid)

    def add_answer(self, tid, session_id, question, answer):
        with self.lock:
            t = self.threads.setdefault(tid, {"session_id": None, "messages": []})
            t["session_id"] = session_id or t["session_id"]
            t["messages"].append({"question": question, "answer": answer, "asked_at": now()})
            self.write()
            return self.thread_record(tid)

    @property
    def forks(self):
        """True when a thread's first question forks the session that wrote the review."""
        return self.agent.can_fork and bool(self.session_id)

    def prompt(self, tid, question, first):
        """The text sent to the agent. The first question in a thread carries what the reviewer is looking at.

        Without a fork the agent hasn't seen the analysis, so the branch overview goes in too. A thread
        whose session was deleted (the review was finished, then reopened) starts a new session, so its
        earlier questions and answers are replayed in the prompt.
        """
        if not first:
            return f"[Follow-up from the i-read-code review page]\n\n{question}"
        m = self.data["meta"]
        intro = "" if self.forks else (
            f"The branch {m['head']} is being compared against {m['base']}. "
            f"Overview from the review:\n{self.data['overview']}\n\n")
        if tid == GENERAL:
            where = "They are asking about the branch as a whole."
        else:
            c = self.changes[tid]
            span = "whole file" if c["start"] is None else f"{c['side']} lines {c['start']}-{c['end']}"
            where = (f"They are looking at review item {tid}: \"{c['title']}\" in {c['file']} ({span}).\n\n"
                     f"Explanation shown to them:\n{c['explanation']}")
            if c.get("concerns"):
                where += f"\n\nConcerns shown to them:\n{c['concerns']}"
        earlier = self.threads.get(tid, {}).get("messages")
        if earlier:
            where += "\n\nEarlier in this thread:\n" + "\n\n".join(
                f"Q: {m['question']}\nA: {m['answer']}" for m in earlier)
        return f"[Question from the i-read-code review page]\n\n{intro}{where}\n\nTheir question:\n{question}"


class Asker:
    """Runs the agent's CLI for questions from the page and yields what it streams back."""

    def __init__(self, agent, cli, cwd):
        self.agent = agent
        self.cli = cli
        self.cwd = cwd
        self.procs = set()
        self.lock = threading.Lock()

    def run(self, prompt, resume, fork):
        """Yields ("session", id), ("text", delta), ("status", line), then ("answer", text) or ("error", msg)."""
        cmd, stdin = self.agent.command(self.cli, prompt, resume, fork, self.cwd)
        with tempfile.TemporaryFile(mode="w+") as stderr:
            proc = subprocess.Popen(cmd, cwd=self.cwd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=stderr, text=True, encoding="utf-8")
            with self.lock:
                self.procs.add(proc)
            timer = threading.Timer(ASK_TIMEOUT, proc.kill)
            timer.start()
            result = None
            state = {}
            streamed = []
            try:
                if stdin is not None:
                    proc.stdin.write(stdin)
                    proc.stdin.close()
                for line in proc.stdout:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("type") == "result":
                        result = ev
                        continue
                    for kind, value in self.agent.parse(ev, state):
                        if kind == "text":
                            streamed.append(value)
                        yield kind, value
                proc.wait()
            finally:
                timer.cancel()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                proc.stdout.close()
                with self.lock:
                    self.procs.discard(proc)
            if result and result.get("subtype") == "success" and not result.get("is_error"):
                yield "session", result.get("session_id")
                text = "".join(streamed) if self.agent.answer_from_stream else ""
                yield "answer", text or result.get("result") or ""
            else:
                stderr.seek(0)
                detail = (result or {}).get("result") or stderr.read().strip()[-500:] \
                    or f"{os.path.basename(self.cli)} exited with code {proc.returncode}"
                yield "error", detail

    def stop_all(self):
        with self.lock:
            for proc in self.procs:
                proc.kill()


def make_handler(review, server_ref, asker):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, ctype="application/json"):
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            route = self.path.split("?")[0]
            if route in ("/", "/index.html"):
                with open(os.path.join(review.dir, "index.html"), "rb") as fh:
                    self.send(200, fh.read(), "text/html; charset=utf-8")
            elif route == "/api/state":
                with review.lock:
                    doc = review.document()
                doc["ask"] = {"available": bool(asker.cli), "forked": review.forks, "agent": review.agent.label}
                self.send(200, doc)
            else:
                self.send(404, {"error": "not found"})

        def do_POST(self):
            route = self.path.split("?")[0]
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self.send(400, {"error": "invalid JSON"})
            if route == "/api/decision":
                try:
                    doc = review.decide(body.get("change_id"), body.get("status"), body.get("reason"))
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
                c = review.changes[body["change_id"]]
                where = c["file"] + (f":{c['start']}-{c['end']}" if c["start"] is not None else "")
                print(f"{body['status']:<9} {where}  {c['title']}", flush=True)
                self.send(200, doc)
            elif route == "/api/decisions":
                try:
                    doc = review.decide_many(body.get("change_ids"), body.get("status"))
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
                print(f"{body['status']:<9} {len(body['change_ids'])} items in bulk", flush=True)
                self.send(200, doc)
            elif route == "/api/ask":
                self.ask(body)
            elif route == "/api/finish":
                asker.stop_all()  # before deleting sessions, so a running answer can't write to one
                doc, removed = review.finish()
                self.send(200, doc)
                c = doc["counts"]
                print(f"Review finished: {c['approved']} approved, {c['declined']} declined, "
                      f"{c['pending']} pending. Saved to {review.path}", flush=True)
                if removed:
                    print(f"Deleted {removed} question session(s)", flush=True)
                threading.Timer(0.3, server_ref[0].shutdown).start()
            else:
                self.send(404, {"error": "not found"})

        def ask(self, body):
            """Streams the answer as JSON lines: status/text events, then done (with the saved thread) or error."""
            if not asker.cli:
                return self.send(503, {"error": f"The {review.agent.label} CLI wasn't found, so questions can't be answered."})
            question = (body.get("question") or "").strip()
            if not question:
                return self.send(400, {"error": "the question is empty"})
            tid = body.get("change_id") or GENERAL
            try:
                claimed, thread_session = review.claim_thread(tid)
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            if not claimed:
                return self.send(409, {"error": "Still answering the previous question here."})
            events = None
            try:
                resume = thread_session or (review.session_id if review.forks else None)
                prompt = review.prompt(tid, question, first=not thread_session)
                print(f"question  {tid}  {question[:80]}", flush=True)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                session = None
                events = asker.run(prompt, resume, fork=not thread_session)
                for kind, value in events:
                    if kind == "session":
                        session = value or session
                        review.track(value)
                    elif kind == "answer":
                        self.event({"type": "done", "thread": review.add_answer(tid, session, question, value)})
                    else:
                        self.event({"type": kind, kind: value})
            except (BrokenPipeError, ConnectionResetError):
                pass  # the page went away; closing the generator below kills the agent process
            except Exception as e:  # e.g. output the parser didn't expect; tell the page instead of going silent
                print(f"error     {tid}  {e!r}", flush=True)
                try:
                    self.event({"type": "error", "error": f"Couldn't read the agent's answer: {e}"})
                except OSError:
                    pass
            finally:
                if events:
                    events.close()
                review.release_thread(tid)

        def event(self, obj):
            self.wfile.write((json.dumps(obj) + "\n").encode())
            self.wfile.flush()

    return Handler


def pick_port(host, preferred):
    for port in ([preferred] if preferred else list(range(8765, 8800))):
        with socket.socket() as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="review directory containing data.json and index.html")
    ap.add_argument("--port", type=int, default=0, help="port (default: first free one from 8765)")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    ap.add_argument("--agent", choices=sorted(agents.AGENTS),
                    help="agent that answers questions (default: the one that started this server, "
                         "else the first CLI found)")
    ap.add_argument("--cli", help="path or name of that agent's CLI (default: claude / cursor-agent)")
    ap.add_argument("--session", help="session to fork for questions (default: the agent's own, "
                                      "e.g. $CLAUDE_CODE_SESSION_ID)")
    ap.add_argument("--finish", action="store_true",
                    help="mark the review finished and delete its question sessions, then exit (no server)")
    args = ap.parse_args()

    d = os.path.abspath(args.dir)
    if not os.path.exists(os.path.join(d, "data.json")):
        sys.exit(f"error: {d}/data.json not found; run build_review.py first")
    agent = agents.AGENTS[args.agent] if args.agent else agents.detect(os.environ)
    review = Review(d, agent, args.session or agent.own_session(os.environ))
    if args.finish:
        doc, removed = review.finish()
        c = doc["counts"]
        print(f"Review finished: {c['approved']} approved, {c['declined']} declined, {c['pending']} pending. "
              f"Deleted {removed} question session(s). Saved to {review.path}")
        return
    asker = Asker(agent, agents.find_cli(agent, args.cli), review.data["meta"]["repo"])
    host = "127.0.0.1"
    server_ref = []
    server = ThreadingHTTPServer((host, pick_port(host, args.port)), make_handler(review, server_ref, asker))
    server_ref.append(server)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"Review page: {url}", flush=True)
    print(f"Decisions are saved to: {review.path}", flush=True)
    if not asker.cli:
        print(f"Questions are off: {args.cli or ' / '.join(agent.clis)} not found on PATH", flush=True)
    elif review.forks:
        print(f"Questions go to {agent.label}, forking session {review.session_id}", flush=True)
    else:
        print(f"Questions go to {agent.label} in new sessions that get the analysis in the prompt", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
