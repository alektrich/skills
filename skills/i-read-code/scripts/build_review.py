#!/usr/bin/env python3
"""Build the interactive review page from changes.json and Claude's analysis.

  python3 build_review.py --dir <review-dir> [--analysis a.json b.json ...]

Reads <dir>/changes.json and the analysis file(s) (default <dir>/analysis*.json),
validates them, adds review items for any changed lines the analysis did not
cover, loads unchanged "context" files referenced by connections, and writes
<dir>/data.json and <dir>/index.html. Exits non-zero with a list of problems if
the analysis can't be used as is. Standard library only.
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "..", "assets", "review.html")
MAX_CONTEXT_BYTES = 400_000
IMPORTANCE = ("high", "medium", "low")


def die(problems):
    print("Analysis has problems; fix them and run build_review.py again:", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    sys.exit(1)


def merge_analyses(paths):
    merged = {"overview": [], "files": {}, "changes": [], "connections": {}, "review_order": []}
    for p in paths:
        try:
            a = json.load(open(p))
        except ValueError as e:
            die([f"{p} is not valid JSON: {e}"])
        if a.get("overview"):
            merged["overview"].append(a["overview"].strip())
        merged["files"].update(a.get("files") or {})
        merged["changes"] += a.get("changes") or []
        for path, conns in (a.get("connections") or {}).items():
            merged["connections"].setdefault(path, []).extend(conns or [])
        merged["review_order"] += [x for x in a.get("review_order") or [] if x not in merged["review_order"]]
    merged["overview"] = "\n\n".join(merged["overview"])
    return merged


def walk(hunk):
    """Yield (line, new_anchor, old_anchor). Removed lines anchor to the new line
    they sit before; added lines anchor to the old line they sit before."""
    o, n = hunk["old_start"], hunk["new_start"]
    if hunk["old_lines"] == 0:
        o += 1
    if hunk["new_lines"] == 0:
        n += 1
    for ln in hunk["lines"]:
        kind = ln[0]
        yield ln, (ln[2] if kind != "del" else n), (ln[1] if kind != "add" else o)
        if kind != "add":
            o += 1
        if kind != "del":
            n += 1


def covers(change, kind, new_anchor, old_anchor):
    s, e = change["start"], change["end"]
    if change["side"] == "new":
        return s <= new_anchor <= e if kind != "del" else (s <= new_anchor and e >= new_anchor - 1)
    if change["side"] == "old":
        return s <= old_anchor <= e if kind != "add" else (s <= old_anchor and e >= old_anchor - 1)
    return False


def auto_item(f, title, explanation, start=None, end=None, side="file"):
    return {"file": f["path"], "side": side, "start": start, "end": end, "title": title,
            "explanation": explanation, "importance": "low", "auto": True}


def fill_gaps(f, changes):
    """Review items for everything in this file the analysis did not explain."""
    if f["binary"]:
        return [] if changes else [auto_item(f, "Binary file changed",
                                             "Binary content can't be shown. Confirm the file is supposed to change.")]
    if f["generated"]:
        return [] if changes else [auto_item(f, "Generated or lock file",
                                             f"+{f['additions']} -{f['deletions']} lines. This looks generated "
                                             "(lock file, build output or snapshot). Confirm the regeneration is expected.")]
    items = []
    if any(c["side"] == "file" for c in changes):
        return items  # a whole-file item already covers every line
    if not f["hunks"]:
        if not changes:
            what = f"Renamed from `{f['old_path']}` with no content changes." if f["old_path"] else "File mode changed."
            items.append(auto_item(f, "Rename" if f["old_path"] else "Mode change", what))
        return items
    for h in f["hunks"]:
        run = None
        for ln, na, oa in walk(h):
            kind = ln[0]
            if kind != "ctx" and not ln[3].strip():
                continue  # added/removed blank lines don't need an explanation
            missed = kind != "ctx" and not any(covers(c, kind, na, oa) for c in changes)
            if missed:
                if run is None:
                    run = {"new": [], "old": []}
                if ln[2] is not None:
                    run["new"].append(ln[2])
                if ln[1] is not None:
                    run["old"].append(ln[1])
            elif run is not None:
                items.append(gap_item(f, run, h))
                run = None
        if run is not None:
            items.append(gap_item(f, run, h))
    return items


def gap_item(f, run, hunk):
    if run["new"]:
        s, e, side = min(run["new"]), max(run["new"]), "new"
    else:
        s, e, side = min(run["old"]), max(run["old"]), "old"
    n = len(run["new"]) + len(run["old"])
    return auto_item(f, "Change without an explanation",
                     f"{n} changed line(s) that the analysis did not describe. Read the diff and decide.",
                     s, e, side)


def load_context(meta, path):
    root = meta["repo"]
    if meta.get("head_ref"):
        r = subprocess.run(["git", "show", f"{meta['head_ref']}:{path}"], cwd=root, capture_output=True)
        data = r.stdout if r.returncode == 0 else None
    else:
        try:
            with open(os.path.join(root, path), "rb") as fh:
                data = fh.read()
        except OSError:
            data = None
    if data is None:
        return None
    if len(data) > MAX_CONTEXT_BYTES or b"\0" in data[:8000]:
        return {"path": path, "content": None, "too_large": True}
    return {"path": path, "content": data.decode("utf-8", errors="replace"), "too_large": False}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="review directory written by collect_changes.py")
    ap.add_argument("--analysis", nargs="*", help="analysis JSON file(s); default <dir>/analysis*.json")
    args = ap.parse_args()

    d = os.path.abspath(args.dir)
    changes_doc = json.load(open(os.path.join(d, "changes.json")))
    meta, files = changes_doc["meta"], changes_doc["files"]
    paths = args.analysis or sorted(glob.glob(os.path.join(d, "analysis*.json")))
    if not paths:
        die([f"no analysis file found in {d} (expected analysis.json)"])
    a = merge_analyses(paths)

    by_path = {f["path"]: f for f in files}
    old_paths = {f["old_path"]: f["path"] for f in files if f["old_path"]}
    problems, warnings = [], []

    # Validate and normalise Claude's change items.
    seen_ids, per_file = set(), {p: [] for p in by_path}
    for k, c in enumerate(a["changes"]):
        label = f"changes[{k}] ({c.get('id') or c.get('title') or 'untitled'})"
        path = c.get("file")
        if path in old_paths:
            path = c["file"] = old_paths[path]
        if path not in by_path:
            problems.append(f"{label}: file '{path}' is not one of the changed files")
            continue
        cid = str(c.get("id") or f"c{k + 1}")
        if cid in seen_ids:
            problems.append(f"{label}: duplicate id '{cid}'")
            continue
        seen_ids.add(cid)
        c["id"] = cid
        if not c.get("title") or not c.get("explanation"):
            problems.append(f"{label}: needs both a title and an explanation")
        c["importance"] = c.get("importance") if c.get("importance") in IMPORTANCE else "medium"
        c["side"] = c.get("side") or ("old" if by_path[path]["status"] == "deleted" else "new")
        if c["side"] == "file":
            c["start"] = c["end"] = None
        else:
            try:
                c["start"] = int(c["start"])
                c["end"] = int(c.get("end") or c["start"])
            except (KeyError, TypeError, ValueError):
                problems.append(f"{label}: start/end must be line numbers (or set side to \"file\")")
                continue
            if c["end"] < c["start"]:
                c["start"], c["end"] = c["end"], c["start"]
            if c["side"] not in ("new", "old"):
                problems.append(f"{label}: side must be \"new\", \"old\" or \"file\"")
                continue
            hits = any(covers(c, ln[0], na, oa) and ln[0] != "ctx"
                       for h in by_path[path]["hunks"] for ln, na, oa in walk(h))
            if not hits:
                warnings.append(f"{label}: lines {c['start']}-{c['end']} ({c['side']}) touch no changed line in {path}; "
                                "it will be shown as a file-level note")
        c["auto"] = False
        per_file[path].append(c)
    if problems:
        die(problems)

    # Cover everything the analysis skipped.
    auto_count = 0
    for path, f in by_path.items():
        for item in fill_gaps(f, per_file[path]):
            auto_count += 1
            item["id"] = f"auto-{auto_count}"
            per_file[path].append(item)
    for path in per_file:
        per_file[path].sort(key=lambda c: (c["start"] is not None, c["start"] or 0))

    # Connections: changed files and unchanged context files, linked both ways.
    context, conns = {}, {}
    def add_conn(src, dst, reason, lines):
        lst = conns.setdefault(src, [])
        if any(x["path"] == dst for x in lst):
            return
        lst.append({"path": dst, "reason": reason, "lines": lines})
    for src, items in a["connections"].items():
        src = old_paths.get(src, src)
        if src not in by_path:
            warnings.append(f"connections: '{src}' is not a changed file; its links were skipped")
            continue
        for item in items:
            dst = old_paths.get(item.get("path"), item.get("path"))
            if not dst or dst == src:
                continue
            if dst not in by_path and dst not in context:
                loaded = load_context(meta, dst)
                if loaded is None:
                    warnings.append(f"connections: '{dst}' (linked from {src}) does not exist; skipped")
                    continue
                context[dst] = loaded
            lines = [[int(r[0]), int(r[-1])] for r in item.get("lines") or [] if r]
            reason = (item.get("reason") or "").strip() or "Related file"
            add_conn(src, dst, reason, lines)
            add_conn(dst, src, reason, [])

    order = [p for p in a["review_order"] if p in by_path]
    order += sorted(p for p in by_path if p not in order and not by_path[p]["generated"])
    order += sorted(p for p in by_path if p not in order)
    out_files = []
    for p in order:
        f = dict(by_path[p])
        info = a["files"].get(p) or a["files"].get(f.get("old_path") or "") or {}
        f["summary"] = info.get("summary", "") if isinstance(info, dict) else str(info)
        out_files.append(f)

    data = {
        "meta": meta,
        "overview": a["overview"],
        "files": out_files,
        "context_files": sorted(context.values(), key=lambda x: x["path"]),
        "changes": [c for p in order for c in per_file[p]],
        "connections": conns,
        "built_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    review = os.path.join(d, "review.json")
    if os.path.exists(review):
        hist = os.path.join(d, "history")
        os.makedirs(hist, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.move(review, os.path.join(hist, f"review-{stamp}.json"))
        warnings.append(f"previous review.json moved to history/review-{stamp}.json")

    with open(os.path.join(d, "data.json"), "w") as fh:
        json.dump(data, fh)
    payload = json.dumps(data).replace("<", "\\u003c")
    html = open(TEMPLATE).read().replace("__REVIEW_DATA__", payload)
    with open(os.path.join(d, "index.html"), "w") as fh:
        fh.write(html)

    explained = sum(1 for c in data["changes"] if not c["auto"])
    print(f"Built {os.path.join(d, 'index.html')}")
    print(f"  {len(out_files)} changed files, {len(context)} context files, "
          f"{explained} explained review items, {auto_count} automatic items")
    for w in warnings:
        print(f"  warning: {w}")
    gaps = [c for c in data["changes"] if c["auto"] and c["title"] == "Change without an explanation"]
    if gaps:
        print(f"  {len(gaps)} item(s) cover lines the analysis skipped:")
        for c in gaps[:15]:
            print(f"    {c['file']} {c['side']} {c['start']}-{c['end']}")


if __name__ == "__main__":
    main()
