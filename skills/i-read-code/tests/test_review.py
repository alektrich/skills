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


def run(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


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

    def serve(self):
        server = subprocess.Popen(
            [sys.executable, os.path.join(SCRIPTS, "serve.py"), "--dir", self.out, "--no-open"],
            stdout=subprocess.PIPE, text=True,
        )
        url = server.stdout.readline().split("Review page: ")[1].strip()

        def post(path, body):
            req = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=5))

        return server, post

    def stop(self, server):
        if server.poll() is None:
            server.kill()
        server.stdout.close()

    def test_bulk_confirm_and_undo(self):
        server, post = self.serve()
        try:
            doc = post("api/decisions", {"change_ids": ["b1", "b3"], "status": "confirmed"})
            self.assertEqual(doc["counts"]["confirmed"], 2)
            doc = post("api/decisions", {"change_ids": ["b3"], "status": "pending"})
            items = {i["change_id"]: i for i in doc["items"]}
            self.assertEqual((items["b1"]["status"], items["b3"]["status"]), ("confirmed", "pending"))
            for bad in ({"change_ids": ["a"], "status": "declined"}, {"change_ids": ["nope"], "status": "confirmed"},
                        {"change_ids": [], "status": "confirmed"}):
                with self.assertRaises(urllib.error.HTTPError) as err:
                    post("api/decisions", bad)
                self.assertEqual(err.exception.code, 400)
                err.exception.close()
        finally:
            self.stop(server)
        with open(os.path.join(self.out, "review.json")) as fh:
            self.assertEqual(json.load(fh)["counts"]["confirmed"], 1)

    def test_confirm_and_decline_are_saved_to_review_json(self):
        server, post = self.serve()
        try:
            post("api/decision", {"change_id": "a", "status": "confirmed"})
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
        self.assertEqual(review["counts"], {"total": 3, "confirmed": 1, "declined": 1, "pending": 1})
        self.assertEqual(items["a"]["status"], "confirmed")
        declined = items["b1"]
        self.assertEqual((declined["status"], declined["reason"]), ("declined", "Import inside main()"))
        self.assertEqual((declined["file"], declined["start"], declined["end"]), ("b.py", 1, 1))
        self.assertEqual(items["b3"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
