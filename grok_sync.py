#!/usr/bin/env python3
"""
grok_sync.py — Incremental sync of Grok (xAI) export into Obsidian vault.

Export: accounts.x.ai/data → "Download account data"
Format: ZIP containing JSON (schema not publicly documented by xAI).

This script handles the most common observed patterns:
  Pattern A (conversation list):
    [{ "id": "...", "title": "...", "create_time": ..., "update_time": ...,
       "messages": [{ "role": "human"|"assistant", "content": "...", "create_time": ... }] }]

  Pattern B (nested under a key):
    { "conversations": [...] }

  Pattern C (xAI account data bundle — conversations in grok_conversations.json or similar)

Since xAI hasn't published a schema, the parser is deliberately defensive.

Vault layout:  <vault>/Grok/conversations/<date> <title>.md
"""

import json, logging, re, shutil, sys, tempfile, zipfile
from datetime import datetime, timezone
from pathlib import Path

SERVICE    = "Grok"
STATE_FILE = ".sync_state.json"
LOG_FILE   = "sync.log"
CONV_DIR   = "general"

ROLE_LABELS = {
    "human": "**You**", "user": "**You**",
    "assistant": "**Grok**", "grok": "**Grok**",
}


def ts_to_iso(ts) -> str:
    if ts is None: return ""
    # Handle ISO string or Unix timestamp
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except: return ts
    try: return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except: return ""

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

def extract_conversations(data) -> list:
    """Defensively extract a list of conversation dicts from any known Grok export shape."""
    if isinstance(data, list):
        # Direct array of conversations
        if data and isinstance(data[0], dict) and ("messages" in data[0] or "title" in data[0]):
            return data
    if isinstance(data, dict):
        # Try common top-level keys
        for key in ("conversations", "grok_conversations", "chats", "data"):
            if key in data and isinstance(data[key], list):
                return data[key]
        # Single conversation object
        if "messages" in data or "title" in data:
            return [data]
    return []

def conv_to_md(conv: dict) -> str:
    title       = (conv.get("title") or conv.get("name") or "Untitled").strip()
    create_time = ts_to_iso(conv.get("create_time") or conv.get("created_at"))
    update_time = ts_to_iso(conv.get("update_time") or conv.get("updated_at") or conv.get("create_time"))
    conv_id     = str(conv.get("id", ""))

    lines = [
        "---",
        f'title: "{title.replace(chr(34), chr(39))}"',
        f"created: {create_time}",
        f"updated: {update_time}",
        f"source: grok",
        f"grok_id: {conv_id}",
        "tags:", "  - grok", "---", "", f"# {title}", "",
    ]

    messages = conv.get("messages") or conv.get("turns") or []
    for msg in messages:
        role    = (msg.get("role") or msg.get("author") or "").lower()
        content = msg.get("content") or msg.get("text") or msg.get("message") or ""
        if isinstance(content, list): content = "\n".join(str(c) for c in content)
        content = content.strip()
        ts      = ts_to_iso(msg.get("create_time") or msg.get("timestamp") or msg.get("created_at"))
        label   = ROLE_LABELS.get(role, f"**{role.capitalize() or 'Unknown'}**")
        if not content: continue
        lines += [f"### {label}" + (f" <small>{ts}</small>" if ts else ""), "", content, "", "---", ""]

    return "\n".join(lines)

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
    log = logging.getLogger("grok")
    log.setLevel(logging.INFO)
    for h in handlers: log.addHandler(h)
    log.info("=== Grok Sync started ===")

    export_p = Path(export_path).expanduser().resolve()
    tmpdir = None
    if export_p.suffix.lower() == ".zip":
        tmpdir = tempfile.mkdtemp(prefix="grok_sync_")
        with zipfile.ZipFile(export_p, "r") as zf: zf.extractall(tmpdir)
        export_dir = Path(tmpdir)
    elif export_p.is_dir():
        export_dir = export_p
    elif export_p.suffix.lower() == ".json":
        export_dir = export_p.parent
    else:
        log.error(f"Not a ZIP/directory/JSON: {export_p}"); return {"new":0,"updated":0,"skipped":0,"errors":1}

    # Find the conversations JSON — try known names first, then largest JSON
    conv_file = None
    for name in ["grok_conversations.json", "conversations.json", "chats.json", "data.json"]:
        c = export_dir / name
        if c.exists(): conv_file = c; break
    if not conv_file:
        # Also search one level deep
        for name in ["grok_conversations.json", "conversations.json", "chats.json"]:
            candidates = list(export_dir.rglob(name))
            if candidates: conv_file = candidates[0]; break
    if not conv_file:
        candidates = list(export_dir.rglob("*.json"))
        if candidates: conv_file = max(candidates, key=lambda x: x.stat().st_size)
    if not conv_file and export_p.suffix.lower() == ".json":
        conv_file = export_p
    if not conv_file:
        log.error("No JSON file found in export."); return {"new":0,"updated":0,"skipped":0,"errors":1}

    log.info(f"  Reading {conv_file.name}")
    with open(conv_file, encoding="utf-8") as f: raw = json.load(f)
    conversations = extract_conversations(raw)
    log.info(f"  {len(conversations)} conversations found")

    if not conversations:
        log.warning("  No conversations parsed — export format may have changed. Check the JSON manually.")
        return {"new":0,"updated":0,"skipped":0,"errors":0}

    state = load_state(vault_dir) if not force else {"last_run": None, "conversations": {}}
    known = state.get("conversations", {})
    counts = {"new": 0, "updated": 0, "skipped": 0, "errors": 0}

    for conv in conversations:
        conv_id     = str(conv.get("id", id(conv)))
        title       = (conv.get("title") or conv.get("name") or "Untitled").strip()
        update_time = conv.get("update_time") or conv.get("updated_at") or conv.get("create_time") or conv.get("created_at")
        create_time = conv.get("create_time") or conv.get("created_at")

        try:
            prev = known.get(conv_id)
            if not force and prev:
                prev_ut = prev.get("update_time")
                cur_ut  = ts_to_iso(update_time)
                if prev_ut and cur_ut and cur_ut <= prev_ut:
                    counts["skipped"] += 1; continue

            if not dry_run: conv_dir.mkdir(parents=True, exist_ok=True)

            date_prefix = ""
            if create_time:
                iso = ts_to_iso(create_time)
                if iso:
                    try:
                        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                        date_prefix = dt.strftime("%Y-%m-%d") + " "
                    except: pass

            stem = safe_filename(f"{date_prefix}{title}")
            if prev and prev.get("filename"):
                out_path = conv_dir / prev["filename"]
            else:
                out_path = unique_path(conv_dir, stem)

            if not dry_run: out_path.write_text(conv_to_md(conv), encoding="utf-8")
            action = "updated" if prev else "new"
            counts[action] += 1
            log.info(f"  [{action.upper():7}]  {out_path.name}")
            known[conv_id] = {"update_time": ts_to_iso(update_time), "filename": out_path.name}
        except Exception as e:
            counts["errors"] += 1; log.error(f"  [ERROR]    {title!r}: {e}")

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
