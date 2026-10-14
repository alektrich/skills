#!/usr/bin/env python3
"""Collect the changes on the current branch relative to a base ref.

Writes two files into <repo>/.i-read-code/<branch-slug>/ (or --out):
  changes.json  structured diff that build_review.py turns into the review page
  diff.txt      the same diff with OLD/NEW line numbers on every line, for reading

By default the comparison is merge-base(base, HEAD) -> working tree, so committed,
staged, unstaged and untracked changes are all included. Standard library only.
"""

import argparse
import codecs
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys

MAX_CONTENT_BYTES = 400_000
BASE_CANDIDATES = ("main", "master", "origin/main", "origin/master")
GENERATED_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "composer.lock",
    "Gemfile.lock", "Cargo.lock", "poetry.lock", "uv.lock", "Pipfile.lock", "go.sum",
    "mix.lock", "pubspec.lock", "Podfile.lock", "flake.lock",
}
GENERATED_PATTERNS = [
    re.compile(p) for p in (
        r"\.min\.(js|css)$", r"\.lock$", r"\.map$", r"(^|/)(dist|build|vendor|node_modules)/",
        r"(^|/)__snapshots__/", r"\.snap$", r"\.pb\.go$", r"_pb2(_grpc)?\.py$", r"\.generated\.",
    )
]
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


def run(args, cwd, check=True):
    r = subprocess.run(args, cwd=cwd, capture_output=True)
    if check and r.returncode != 0:
        sys.exit(f"error: {' '.join(args)} failed:\n{r.stderr.decode(errors='replace').strip()}")
    return r


def git(cwd, *args, check=True):
    out = run(["git", "-c", "core.quotePath=false", *args], cwd, check).stdout
    return out.decode("utf-8", errors="replace")


def rev(cwd, ref):
    r = run(["git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"], cwd, check=False)
    return r.stdout.decode().strip() if r.returncode == 0 else None


def detect_base(cwd):
    for cand in BASE_CANDIDATES:
        if rev(cwd, cand):
            return cand
    head = git(cwd, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False).strip()
    if head and rev(cwd, head):
        return head
    sys.exit("error: could not find a main or master branch. Pass --base <ref> to choose what to compare against.")


def unquote(path):
    """Undo git's C-style quoting of unusual paths ("a/x\\ty" -> a/x<TAB>y)."""
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        raw = codecs.decode(path[1:-1], "unicode_escape")
        return raw.encode("latin-1", errors="ignore").decode("utf-8", errors="replace")
    return path


def strip_prefix(path):
    path = unquote(path.strip())
    return path[2:] if path[:2] in ("a/", "b/") else path


def header_paths(line):
    """Best-effort split of 'diff --git a/X b/Y' when no ---/+++ or rename lines exist."""
    rest = line[len("diff --git "):]
    if rest.startswith('"'):
        m = re.match(r'("(?:[^"\\]|\\.)*")\s+(.*)$', rest)
        if m:
            return strip_prefix(m.group(1)), strip_prefix(m.group(2))
    n = (len(rest) - 1) // 2
    return strip_prefix(rest[:n]), strip_prefix(rest[n + 1:])


def parse_patch(text):
    files = []
    chunks = re.split(r"^(?=diff --git )", text, flags=re.M)
    for chunk in chunks:
        if not chunk.startswith("diff --git "):
            continue
        lines = chunk.split("\n")
        f = {"path": None, "old_path": None, "status": "modified", "binary": False,
             "hunks": [], "mode_change": False}
        old_p = new_p = None
        i = 1
        while i < len(lines) and not lines[i].startswith("@@"):
            ln = lines[i]
            if ln.startswith("new file mode"):
                f["status"] = "added"
            elif ln.startswith("deleted file mode"):
                f["status"] = "deleted"
            elif ln.startswith("rename from "):
                old_p, f["status"] = unquote(ln[12:]), "renamed"
            elif ln.startswith("rename to "):
                new_p = unquote(ln[10:])
            elif ln.startswith("copy from "):
                old_p, f["status"] = unquote(ln[10:]), "copied"
            elif ln.startswith("copy to "):
                new_p = unquote(ln[8:])
            elif ln.startswith("old mode ") or ln.startswith("new mode "):
                f["mode_change"] = True
            elif ln.startswith("--- ") and ln[4:] != "/dev/null":
                old_p = old_p or strip_prefix(ln[4:])
            elif ln.startswith("+++ ") and ln[4:] != "/dev/null":
                new_p = new_p or strip_prefix(ln[4:])
            elif ln.startswith("Binary files ") or ln.startswith("GIT binary patch"):
                f["binary"] = True
            i += 1
        if old_p is None and new_p is None:
            old_p, new_p = header_paths(lines[0])
            if f["status"] == "added":
                old_p = None
            elif f["status"] == "deleted":
                new_p = None
        f["path"] = new_p or old_p
        f["old_path"] = old_p if old_p and old_p != f["path"] else None

        hunk = None
        for ln in lines[i:]:
            m = HUNK_RE.match(ln)
            if m:
                o, n = int(m.group(1)), int(m.group(3))
                hunk = {"old_start": o, "old_lines": int(m.group(2) or 1), "new_start": n,
                        "new_lines": int(m.group(4) or 1), "header": m.group(5).strip(), "lines": []}
                f["hunks"].append(hunk)
                o_no, n_no = o, n
                continue
            if hunk is None or not ln or ln.startswith("\\"):
                continue
            tag, body = ln[0], ln[1:]
            if tag == " ":
                hunk["lines"].append(["ctx", o_no, n_no, body]); o_no += 1; n_no += 1
            elif tag == "-":
                hunk["lines"].append(["del", o_no, None, body]); o_no += 1
            elif tag == "+":
                hunk["lines"].append(["add", None, n_no, body]); n_no += 1
        files.append(f)
    return files


def is_binary_bytes(data):
    return b"\0" in data[:8000]


def untracked_file(root, path):
    full = os.path.join(root, path)
    try:
        with open(full, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    f = {"path": path, "old_path": None, "status": "added", "binary": False, "hunks": [],
         "mode_change": False, "untracked": True}
    if is_binary_bytes(data):
        f["binary"] = True
        return f
    text = data.decode("utf-8", errors="replace")
    body = text.split("\n")
    if body and body[-1] == "":
        body.pop()
    if body:
        f["hunks"].append({"old_start": 0, "old_lines": 0, "new_start": 1, "new_lines": len(body),
                           "header": "", "lines": [["add", None, k + 1, t] for k, t in enumerate(body)]})
    return f


def generated_attr(root, paths):
    if not paths:
        return set()
    r = subprocess.run(["git", "check-attr", "-z", "--stdin", "linguist-generated"], cwd=root,
                       input="\0".join(paths).encode(), capture_output=True)
    parts = r.stdout.decode(errors="replace").split("\0")
    out = set()
    for k in range(0, len(parts) - 2, 3):
        if parts[k + 2] in ("set", "true"):
            out.add(parts[k])
    return out


def looks_generated(path):
    return os.path.basename(path) in GENERATED_NAMES or any(p.search(path) for p in GENERATED_PATTERNS)


def read_content(root, f, merge_base, head_ref):
    """Full text of the file for the full-file view: new side, or old side if deleted."""
    if f["binary"]:
        return None, None, False
    if f["status"] == "deleted":
        side, spec = "old", f"{merge_base}:{f['old_path'] or f['path']}"
    elif head_ref:
        side, spec = "new", f"{head_ref}:{f['path']}"
    else:
        side, spec = "new", None
    if spec:
        r = run(["git", "show", spec], root, check=False)
        data = r.stdout if r.returncode == 0 else None
    else:
        try:
            with open(os.path.join(root, f["path"]), "rb") as fh:
                data = fh.read()
        except OSError:
            data = None
    if data is None:
        return None, side, False
    if len(data) > MAX_CONTENT_BYTES:
        return None, side, True
    return data.decode("utf-8", errors="replace"), side, False


def slugify(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "review"


def ensure_excluded(root):
    """Keep review output out of `git status` without touching the tracked .gitignore."""
    gitdir = git(root, "rev-parse", "--git-common-dir").strip()
    exclude = os.path.join(root, gitdir, "info", "exclude") if not os.path.isabs(gitdir) else os.path.join(gitdir, "info", "exclude")
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    existing = open(exclude).read() if os.path.exists(exclude) else ""
    if ".i-read-code/" not in existing.split():
        with open(exclude, "a") as fh:
            fh.write(("\n" if existing and not existing.endswith("\n") else "") + ".i-read-code/\n")


def write_diff_txt(path, meta, files):
    total_add = sum(f["additions"] for f in files)
    total_del = sum(f["deletions"] for f in files)
    out = [
        f"# i-read-code diff: {meta['head']} vs {meta['base']} (merge-base {meta['merge_base'][:10]})",
        f"# {len(files)} files, +{total_add} -{total_del}.",
        "# Columns: OLD line no, NEW line no, marker (+ added, - removed), code.",
        "# Cite NEW line numbers for changes; cite OLD numbers (side \"old\") only for pure removals.",
        "",
    ]
    for f in files:
        label = f["status"] + (f" from {f['old_path']}" if f["old_path"] else "")
        if f.get("untracked"):
            label += ", untracked"
        out += ["=" * 72, f"FILE {f['path']}  [{label}]  +{f['additions']} -{f['deletions']}", "=" * 72]
        if f["binary"]:
            out += ["(binary file, diff omitted)", ""]
            continue
        if f["generated"]:
            out += ["(generated or lock file, diff omitted; it gets a review item automatically)", ""]
            continue
        if f["mode_change"] and not f["hunks"]:
            out.append("(file mode changed)")
        for h_i, h in enumerate(f["hunks"], 1):
            o_end = h["old_start"] + max(h["old_lines"], 1) - 1
            n_end = h["new_start"] + max(h["new_lines"], 1) - 1
            out.append(f"@@ hunk {h_i}: old {h['old_start']}-{o_end}, new {h['new_start']}-{n_end} @@ {h['header']}".rstrip())
            for kind, o, n, text in h["lines"]:
                mark = {"ctx": " ", "del": "-", "add": "+"}[kind]
                out.append(f"{'' if o is None else o:>6} {'' if n is None else n:>6} {mark} {text}")
        out.append("")
    with open(path, "w") as fh:
        fh.write("\n".join(out))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", help="ref to compare against (default: main, then master, then origin/main, origin/master)")
    ap.add_argument("--head", help="compare this committed ref instead of the working tree")
    ap.add_argument("--committed-only", action="store_true", help="ignore staged, unstaged and untracked changes")
    ap.add_argument("--repo", default=".", help="path inside the git repository (default: cwd)")
    ap.add_argument("--out", help="output directory (default: <repo>/.i-read-code/<branch-slug>)")
    args = ap.parse_args()

    root = git(os.path.abspath(args.repo), "rev-parse", "--show-toplevel").strip()
    base = args.base or detect_base(root)
    base_sha = rev(root, base) or sys.exit(f"error: base ref '{base}' does not exist")

    head_ref = args.head or ("HEAD" if args.committed_only else None)
    head_sha = rev(root, head_ref or "HEAD") or sys.exit(f"error: head ref '{head_ref}' does not exist")
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    head_name = args.head or branch
    if head_name == "HEAD":
        head_name = head_sha[:10]

    notes = []
    mb = run(["git", "merge-base", base_sha, head_sha], root, check=False).stdout.decode().strip()
    if not mb:
        mb = base_sha
        notes.append(f"No common ancestor between {base} and {head_name}; diffing directly against {base}.")
    if mb == head_sha and not head_ref:
        notes.append(f"{head_name} has no commits beyond {base}; only uncommitted changes are included.")

    if not head_ref:
        ensure_excluded(root)
    diff_args = ["diff", "-M", "--no-color", "--no-ext-diff", "--no-textconv", "-U3", mb]
    if head_ref:
        diff_args.append(head_sha)
    files = parse_patch(git(root, *diff_args))
    for f in files:
        f["untracked"] = False
    if not head_ref:
        untracked = [p for p in git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if p]
        for p in untracked:
            f = untracked_file(root, p)
            if f:
                files.append(f)

    if not files:
        sys.exit(f"No changes between {head_name} and {base} (merge-base {mb[:10]}). Nothing to review.")

    gen_attr = generated_attr(root, [f["path"] for f in files])
    for f in files:
        f["generated"] = f["path"] in gen_attr or looks_generated(f["path"])
        f["additions"] = sum(1 for h in f["hunks"] for ln in h["lines"] if ln[0] == "add")
        f["deletions"] = sum(1 for h in f["hunks"] for ln in h["lines"] if ln[0] == "del")
        f["content"], f["content_side"], f["too_large"] = read_content(root, f, mb, head_ref and head_sha)
    files.sort(key=lambda f: f["path"])

    out_dir = args.out or os.path.join(root, ".i-read-code", slugify(head_name))
    os.makedirs(out_dir, exist_ok=True)
    meta = {
        "repo": root, "base": base, "base_sha": base_sha, "merge_base": mb, "head": head_name,
        "head_sha": head_sha, "head_ref": head_ref and head_sha,
        "include_uncommitted": not head_ref, "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "notes": notes,
    }
    write_diff_txt(os.path.join(out_dir, "diff.txt"), meta, files)
    with open(os.path.join(out_dir, "diff.txt"), "rb") as fh:
        meta["fingerprint"] = hashlib.sha1(fh.read()).hexdigest()[:12]
    with open(os.path.join(out_dir, "changes.json"), "w") as fh:
        json.dump({"meta": meta, "files": files}, fh)

    diff_lines = sum(len(h["lines"]) for f in files if not f["generated"] for h in f["hunks"])
    print(f"Comparing {head_name} against {base} (merge-base {mb[:10]})"
          + ("" if head_ref else ", including uncommitted and untracked changes"))
    for n in notes:
        print(f"note: {n}")
    print(f"Output directory: {out_dir}")
    print(f"diff.txt: {diff_lines} diff lines to read")
    for f in files:
        flags = [x for x in ("untracked", "binary", "generated") if f.get(x)]
        rename = f" (from {f['old_path']})" if f["old_path"] else ""
        print(f"  {f['status'][0].upper()} {f['path']}{rename}  +{f['additions']} -{f['deletions']}"
              + (f"  [{', '.join(flags)}]" if flags else ""))
    existing = os.path.join(out_dir, "review.json")
    if os.path.exists(existing):
        try:
            prev = json.load(open(existing))
            c = prev.get("counts", {})
            same = prev.get("fingerprint") == meta["fingerprint"]
            print(f"Existing review found: {c.get('approved', c.get('confirmed', 0))} approved, {c.get('declined', 0)} declined, "
                  f"{c.get('pending', 0)} pending, status {prev.get('status')}, "
                  + ("the diff is unchanged since then." if same else "the diff has changed since then."))
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
