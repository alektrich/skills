#!/usr/bin/env python3
"""Serve the review page and save every decision to <dir>/review.json.

  python3 serve.py --dir <review-dir> [--port N] [--no-open]

Listens on 127.0.0.1 only. Each Confirm/Decline (and each bulk confirm) is written to review.json
immediately (atomic replace), so closing the browser loses nothing; restarting
the server resumes from the saved file. The process exits when the reviewer
clicks "Finish review", which lets whoever started it in the background know
the review is done. Standard library only.
"""

import argparse
import datetime
import json
import os
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATUSES = ("pending", "confirmed", "declined")


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class Review:
    def __init__(self, directory):
        self.dir = directory
        self.path = os.path.join(directory, "review.json")
        self.data = json.load(open(os.path.join(directory, "data.json")))
        self.changes = {c["id"]: c for c in self.data["changes"]}
        self.files = {f["path"]: f for f in self.data["files"]}
        self.lock = threading.Lock()
        saved = {}
        if os.path.exists(self.path):
            try:
                prev = json.load(open(self.path))
                if prev.get("fingerprint") == self.data["meta"].get("fingerprint"):
                    saved = {r["change_id"]: r for r in prev.get("items", [])}
            except (OSError, ValueError):
                pass
        self.status = "in_progress"
        self.items = {}
        for cid, c in self.changes.items():
            r = saved.get(cid) or {}
            self.items[cid] = {"status": r.get("status", "pending"), "reason": r.get("reason", ""),
                               "decided_at": r.get("decided_at")}
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
        }
        if old:
            rec["old_path"] = old
        return rec

    def document(self):
        m = self.data["meta"]
        items = [self.record(cid, c) for cid, c in self.changes.items()]
        counts = {s: sum(1 for i in items if i["status"] == s) for s in STATUSES}
        counts["total"] = len(items)
        return {
            "tool": "i-read-code", "version": 1, "status": self.status, "updated_at": now(),
            "repo": m["repo"], "base": m["base"], "base_sha": m["base_sha"], "merge_base": m["merge_base"],
            "head": m["head"], "head_sha": m["head_sha"], "include_uncommitted": m["include_uncommitted"],
            "fingerprint": m.get("fingerprint"), "counts": counts, "items": items,
        }

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
                               "decided_at": now() if status != "pending" else None}
            self.status = "in_progress"
            return self.write()

    def decide_many(self, cids, status):
        """Bulk confirm (or reset to pending) in one write; declining always needs a per-item reason."""
        if status not in ("confirmed", "pending"):
            raise ValueError("bulk decisions can only confirm or reset to pending")
        if not isinstance(cids, list) or not cids:
            raise ValueError("change_ids must be a non-empty list")
        unknown = [c for c in cids if c not in self.changes]
        if unknown:
            raise ValueError(f"unknown change ids {unknown!r}")
        with self.lock:
            stamp = now() if status != "pending" else None
            for cid in cids:
                self.items[cid] = {"status": status, "reason": "", "decided_at": stamp}
            self.status = "in_progress"
            return self.write()

    def finish(self):
        with self.lock:
            self.status = "finished"
            return self.write()


def make_handler(review, server_ref):
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
                    self.send(200, review.document())
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
            elif route == "/api/finish":
                doc = review.finish()
                self.send(200, doc)
                c = doc["counts"]
                print(f"Review finished: {c['confirmed']} confirmed, {c['declined']} declined, "
                      f"{c['pending']} pending. Saved to {review.path}", flush=True)
                threading.Timer(0.3, server_ref[0].shutdown).start()
            else:
                self.send(404, {"error": "not found"})

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
    args = ap.parse_args()

    d = os.path.abspath(args.dir)
    if not os.path.exists(os.path.join(d, "data.json")):
        sys.exit(f"error: {d}/data.json not found; run build_review.py first")
    review = Review(d)
    host = "127.0.0.1"
    server_ref = []
    server = ThreadingHTTPServer((host, pick_port(host, args.port)), make_handler(review, server_ref))
    server_ref.append(server)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"Review page: {url}", flush=True)
    print(f"Decisions are saved to: {review.path}", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
