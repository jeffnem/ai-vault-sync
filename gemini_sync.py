#!/usr/bin/env python3
"""
gemini_sync.py — Incremental sync of Google Gemini Takeout export into Obsidian vault.

Export: takeout.google.com → select "My Activity > Gemini Apps" → JSON format
File:   Takeout/My Activity/Gemini Apps/MyActivity.json

Gemini Takeout is an ACTIVITY LOG (one entry per prompt/response pair), not a
conversation archive. This script groups entries by conversation ID (extracted
from titleUrl), reconstructs conversations, then writes one .md per conversation.

Two known variants of the entry format are handled:
  Variant A:  entry.details = [{"name": "Request", "value": "..."}, {"name": "Response", "value": "..."}]
  Variant B:  entry.userInteractions = [{"query": "...", "response": "..."}]

Vault layout:  <vault>/Gemini/conversations/<date> <title>.md
"""

import json, logging, re, shutil, sys, tempfile, zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SERVICE    = "Gemini"
STATE_FILE = ".sync_state.json"
LOG_FILE   = "sync.log"
CONV_DIR   = "general"


def ts_to_iso(ts_str) -> str:
    if not ts_str: return ""
    try:
        # Takeout uses ISO 8601 already, but may have fractional seconds
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except: return ts_str

def extract_conv_id(url: str) -> str:
    if not url: return ""
    m = re.search(r"/app/c/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else re.sub(r"[^a-zA-Z0-9_-]", "_", url)[-40:]

def safe_filename(title: str, max_len=80) -> str:
    clean = re.sub(r'[\\/:*?"<>|]', "", title)
    return re.sub(r"\s+", " ", clean).strip()[:max_len] or "Untitled"

def unique_path(directory: Path, stem: str) -> Path:
    p = directory / f"{stem}.md"
    if not p.exists(): return p
    i = 2
    while True:
        p = directory / f"{stem}_{i}.md"
        if not p.exists(): return p
        i += 1

def parse_entry(entry: dict) -> list[dict]:
    """Extract list of {role, text} pairs from one activity entry."""
    messages = []
    # Variant A: details array
    details = entry.get("details", [])
    if details:
        for d in details:
            name  = d.get("name", "")
            value = (d.get("value") or "").strip()
            if not value: continue
            role = "user" if name == "Request" else "assistant"
            messages.append({"role": role, "text": value, "time": entry.get("time", "")})
        return messages
    # Variant B: userInteractions array
    for ui in entry.get("userInteractions", []):
        q = (ui.get("query") or ui.get("prompt") or "").strip()
        r = (ui.get("response") or ui.get("answer") or "").strip()
        ts = entry.get("time", "")
        if q: messages.append({"role": "user",      "text": q, "time": ts})
        if r: messages.append({"role": "assistant",  "text": r, "time": ts})
    return messages

def build_conversations(entries: list) -> dict:
    """Group activity log entries into conversations keyed by conv_id."""
    groups = defaultdict(list)
    for entry in entries:
        url     = entry.get("titleUrl", "")
        conv_id = extract_conv_id(url)
        if not conv_id: conv_id = "unknown"
        groups[conv_id].append(entry)
    # Sort entries within each conversation by time
    for conv_id in groups:
        groups[conv_id].sort(key=lambda e: e.get("time", ""))
    return dict(groups)

def conv_to_md(conv_id: str, entries: list) -> tuple[str, str, str]:
    """Returns (markdown_text, create_time_iso, update_time_iso)."""
    first_entry = entries[0]
    last_entry  = entries[-1]
    create_time = ts_to_iso(first_entry.get("time", ""))
    update_time = ts_to_iso(last_entry.get("time", ""))

    # Derive title from first user message
    title = "Gemini Conversation"
    for entry in entries:
        msgs = parse_entry(entry)
        for m in msgs:
            if m["role"] == "user" and m["text"]:
                title = m["text"][:60].replace("\n", " ").strip()
                if len(m["text"]) > 60: title += "…"
                break
        if title != "Gemini Conversation": break

    lines = [
        "---",
        f'title: "{title.replace(chr(34), chr(39))}"',
        f"created: {create_time}",
        f"updated: {update_time}",
        f"source: gemini",
        f"gemini_id: {conv_id}",
        "tags:", "  - gemini", "---", "", f"# {title}", "",
    ]

    for entry in entries:
        msgs = parse_entry(entry)
        for m in msgs:
            label = "**You**" if m["role"] == "user" else "**Gemini**"
            ts    = ts_to_iso(m.get("time", ""))
            lines += [f"### {label}" + (f" <small>{ts}</small>" if ts else ""), "", m["text"], "", "---", ""]

    return "\n".join(lines), create_time, update_time

def load_state(d: Path) -> dict:
    p = d / STATE_FILE
    if p.exists():
        try:
            with open(p) as f: return json.load(f)
        except: pass
    return {"last_run": None, "conversations": {}}

def save_state(d: Path, state: dict, dry_run: bool):
    if dry_run: return
    state["last_run"] = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(d / STATE_FILE, "w") as f: json.dump(state, f, indent=2)

def sync(export_path: str, vault_root: str, quiet=False, force=False, dry_run=False):
    vault_dir = Path(vault_root) / "AI-Chats" / SERVICE
    vault_dir.mkdir(parents=True, exist_ok=True)
    conv_dir  = vault_dir / CONV_DIR

    handlers = [logging.FileHandler(vault_dir / LOG_FILE, encoding="utf-8")]
    if not quiet: handlers.append(logging.StreamHandler(sys.stdout))
    log = logging.getLogger("gemini")
    log.setLevel(logging.INFO)
    for h in handlers: log.addHandler(h)
    log.info("=== Gemini Sync started ===")

    export_p = Path(export_path).expanduser().resolve()
    tmpdir = None
    if export_p.suffix.lower() in (".zip", ".tgz", ".gz"):
        tmpdir = tempfile.mkdtemp(prefix="gemini_sync_")
        if export_p.suffix.lower() == ".zip":
            with zipfile.ZipFile(export_p, "r") as zf: zf.extractall(tmpdir)
        else:
            import tarfile
            with tarfile.open(export_p) as tf: tf.extractall(tmpdir)
        export_dir = Path(tmpdir)
    elif export_p.is_dir():
        export_dir = export_p
    elif export_p.suffix.lower() == ".json":
        export_dir = export_p.parent
    else:
        log.error(f"Not a ZIP/TGZ/directory/JSON: {export_p}"); return {"new":0,"updated":0,"skipped":0,"errors":1}

    # Find MyActivity.json — may be nested in Takeout/My Activity/Gemini Apps/
    activity_file = None
    for pattern in ["**/MyActivity.json", "**/Gemini*/*.json", "**/*.json"]:
        candidates = list(export_dir.glob(pattern))
        if candidates:
            activity_file = max(candidates, key=lambda x: x.stat().st_size)
            break
    if not activity_file and export_p.suffix.lower() == ".json":
        activity_file = export_p
    if not activity_file:
        log.error("MyActivity.json not found in export."); return {"new":0,"updated":0,"skipped":0,"errors":1}

    log.info(f"  Reading {activity_file.name}")
    with open(activity_file, encoding="utf-8") as f: entries = json.load(f)
    if not isinstance(entries, list): entries = [entries]
    log.info(f"  {len(entries)} activity entries")

    conversations = build_conversations(entries)
    log.info(f"  {len(conversations)} conversations reconstructed")

    state = load_state(vault_dir) if not force else {"last_run": None, "conversations": {}}
    known = state.get("conversations", {})
    counts = {"new": 0, "updated": 0, "skipped": 0, "errors": 0}

    for conv_id, conv_entries in conversations.items():
        try:
            md, create_time, update_time = conv_to_md(conv_id, conv_entries)
            prev = known.get(conv_id)

            if not force and prev:
                if prev.get("update_time") and update_time and update_time <= prev["update_time"]:
                    counts["skipped"] += 1; continue

            if not dry_run: conv_dir.mkdir(parents=True, exist_ok=True)

            date_prefix = ""
            if create_time:
                try:
                    dt = datetime.fromisoformat(create_time.replace("Z", "+00:00"))
                    date_prefix = dt.strftime("%Y-%m-%d") + " "
                except: pass

            # Derive short title for filename from first user message
            title = "Gemini Conversation"
            for entry in conv_entries:
                msgs = parse_entry(entry)
                for m in msgs:
                    if m["role"] == "user" and m["text"]:
                        title = m["text"][:60].replace("\n", " ").strip()
                        break
                if title != "Gemini Conversation": break

            stem = safe_filename(f"{date_prefix}{title}")
            if prev and prev.get("filename"):
                out_path = conv_dir / prev["filename"]
            else:
                out_path = unique_path(conv_dir, stem)

            if not dry_run: out_path.write_text(md, encoding="utf-8")
            action = "updated" if prev else "new"
            counts[action] += 1
            log.info(f"  [{action.upper():7}]  {out_path.name}")
            known[conv_id] = {"update_time": update_time, "filename": out_path.name}
        except Exception as e:
            counts["errors"] += 1; log.error(f"  [ERROR]    {conv_id}: {e}")

    state["conversations"] = known
    save_state(vault_dir, state, dry_run)
    if tmpdir: shutil.rmtree(tmpdir, ignore_errors=True)
    log.info(f"=== Done: {counts['new']} new, {counts['updated']} updated, {counts['skipped']} skipped ===")
    return counts

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("export"); p.add_argument("vault")
    p.add_argument("--quiet", action="store_true"); p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    r = sync(args.export, args.vault, args.quiet, args.force, args.dry_run)
    print(f"\n✅  {r['new']} new  |  {r['updated']} updated  |  {r['skipped']} skipped  |  {r['errors']} errors")
