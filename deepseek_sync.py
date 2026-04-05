#!/usr/bin/env python3
"""
deepseek_sync.py — Incremental sync of DeepSeek export into Obsidian vault.

Export JSON schema (flat array):
  [{ "id": "conv_abc", "title": "...", "create_time": 1700000000,
     "update_time": 1700003600, "model": "deepseek-chat",
     "messages": [{ "role": "user"|"assistant", "content": "...",
                    "create_time": 1700000100 }] }]

Vault layout:  <vault>/DeepSeek/<model-name>/<date> <title>.md
"""

import json, logging, re, shutil, sys, tempfile, zipfile
from datetime import datetime, timezone
from pathlib import Path

SERVICE     = "DeepSeek"
STATE_FILE  = ".sync_state.json"
LOG_FILE    = "sync.log"
ROLE_LABELS = {"user": "**You**", "assistant": "**DeepSeek**"}


def ts_to_iso(ts) -> str:
    if ts is None: return ""
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

def conv_to_md(conv: dict) -> str:
    title  = (conv.get("title") or "Untitled").strip()
    model  = conv.get("model", "")
    lines  = [
        "---",
        f'title: "{title.replace(chr(34), chr(39))}"',
        f"created: {ts_to_iso(conv.get('create_time'))}",
        f"updated: {ts_to_iso(conv.get('update_time'))}",
        f"source: deepseek",
        f"model: {model}",
        f"deepseek_id: {conv.get('id', '')}",
        "tags:", "  - deepseek", "---", "", f"# {title}", "",
    ]
    for msg in conv.get("messages", []):
        role  = msg.get("role", "")
        text  = (msg.get("content") or "").strip()
        ts    = ts_to_iso(msg.get("create_time"))
        label = ROLE_LABELS.get(role, f"**{role.capitalize()}**")
        if not text: continue
        lines += [f"### {label}" + (f" <small>{ts}</small>" if ts else ""), "", text, "", "---", ""]
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

    handlers = [logging.FileHandler(vault_dir / LOG_FILE, encoding="utf-8")]
    if not quiet: handlers.append(logging.StreamHandler(sys.stdout))
    log = logging.getLogger("deepseek")
    log.setLevel(logging.INFO)
    for h in handlers: log.addHandler(h)
    log.info("=== DeepSeek Sync started ===")

    export_p = Path(export_path).expanduser().resolve()
    tmpdir = None
    if export_p.suffix.lower() == ".zip":
        tmpdir = tempfile.mkdtemp(prefix="ds_sync_")
        with zipfile.ZipFile(export_p, "r") as zf: zf.extractall(tmpdir)
        export_dir = Path(tmpdir)
    elif export_p.is_dir():
        export_dir = export_p
    else:
        log.error(f"Not a ZIP or directory: {export_p}"); return {"new":0,"updated":0,"skipped":0,"errors":1}

    conv_file = None
    for name in ["conversations.json", "chat_history.json", "data.json"]:
        c = export_dir / name
        if c.exists(): conv_file = c; break
    if not conv_file:
        candidates = list(export_dir.rglob("*.json"))
        if candidates: conv_file = max(candidates, key=lambda x: x.stat().st_size)
    if not conv_file:
        log.error("No JSON file found in export."); return {"new":0,"updated":0,"skipped":0,"errors":1}

    with open(conv_file, encoding="utf-8") as f: conversations = json.load(f)
    if isinstance(conversations, dict): conversations = list(conversations.values())
    log.info(f"  {len(conversations)} conversations in export")

    state = load_state(vault_dir) if not force else {"last_run": None, "conversations": {}}
    known = state.get("conversations", {})
    counts = {"new": 0, "updated": 0, "skipped": 0, "errors": 0}

    for conv in conversations:
        conv_id     = str(conv.get("id", ""))
        title       = (conv.get("title") or "Untitled").strip()
        update_time = conv.get("update_time") or conv.get("create_time")
        create_time = conv.get("create_time")
        model       = (conv.get("model") or "general").strip().lower()

        try:
            prev = known.get(conv_id)
            if not force and prev:
                if prev.get("update_time") and update_time and float(update_time) <= float(prev["update_time"]):
                    counts["skipped"] += 1; continue

            out_dir = vault_dir / model
            if not dry_run: out_dir.mkdir(parents=True, exist_ok=True)

            date_prefix = ""
            if create_time:
                try: date_prefix = datetime.fromtimestamp(float(create_time), tz=timezone.utc).strftime("%Y-%m-%d") + " "
                except: pass

            stem = safe_filename(f"{date_prefix}{title}")
            if prev and prev.get("filename"):
                out_path = vault_dir / prev.get("subdir", model) / prev["filename"]
            else:
                out_path = unique_path(out_dir, stem)

            if not dry_run: out_path.write_text(conv_to_md(conv), encoding="utf-8")
            action = "updated" if prev else "new"
            counts[action] += 1
            log.info(f"  [{action.upper():7}]  {model}/{out_path.name}")
            known[conv_id] = {"update_time": update_time, "subdir": model, "filename": out_path.name}
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
