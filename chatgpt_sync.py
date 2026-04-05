#!/usr/bin/env python3
"""
chatgpt_sync.py — Incremental sync of ChatGPT export into Obsidian vault.

Export: privacy.openai.com → Download my data → ZIP
  conversations.json  — all chats
  Attachments         — uploaded files

Vault layout:  <vault>/AI-Chats/ChatGPT/<project>/<date> <title>.md
  Project comes from the ChatGPT "project" field on each conversation.
  Falls back to "general" if no project is assigned.
"""

import json, logging, re, shutil, sys, tempfile, zipfile
from datetime import datetime, timezone
from pathlib import Path

SERVICE    = "ChatGPT"
ROOT       = "AI-Chats"
STATE_FILE = ".sync_state.json"
LOG_FILE   = "sync.log"
ATT_SUBDIR = "attachments"
DEFAULT_PROJECT = "general"

ROLE_LABELS = {
    "user":      "**You**",
    "assistant": "**ChatGPT**",
    "system":    "**System**",
    "tool":      "**Tool**",
}


def ts_to_iso(ts) -> str:
    if ts is None: return ""
    try: return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except: return ""

def safe_filename(title: str, max_len=80) -> str:
    clean = re.sub(r'[\\/:*?"<>|]', "", title)
    return re.sub(r"\s+", " ", clean).strip()[:max_len] or "Untitled"

def safe_dirname(name: str, max_len=60) -> str:
    clean = re.sub(r'[\\/:*?"<>|]', "", name)
    return re.sub(r"\s+", " ", clean).strip()[:max_len] or DEFAULT_PROJECT

def unique_path(directory: Path, stem: str) -> Path:
    p = directory / f"{stem}.md"
    if not p.exists(): return p
    i = 2
    while True:
        p = directory / f"{stem}_{i}.md"
        if not p.exists(): return p
        i += 1

def extract_text(content) -> str:
    if isinstance(content, str): return content
    if isinstance(content, dict):
        ct = content.get("content_type", "")
        if ct == "text":
            return "\n".join(p for p in content.get("parts", []) if isinstance(p, str))
        if ct == "multimodal_text":
            parts = content.get("parts", [])
            texts = []
            for p in parts:
                if isinstance(p, str): texts.append(p)
                elif isinstance(p, dict) and p.get("content_type") == "image_asset_pointer":
                    fname = p.get("metadata", {}).get("dalle", {}).get("prompt", "")
                    asset = p.get("asset_pointer", "")
                    texts.append(f"![[{fname or asset}]]")
            return "\n".join(texts)
        if ct in ("code", "execution_output"):
            lang = content.get("language", "")
            return f"```{lang}\n{content.get('text','')}\n```"
        if ct == "tether_quote":
            url = content.get("url", ""); title = content.get("title", ""); body = content.get("text", "")
            return f"> **[{title}]({url})**\n> {body}"
    return ""

def walk_messages(mapping: dict) -> list:
    children_map: dict = {}
    root_id = None
    for node_id, node in mapping.items():
        parent = node.get("parent")
        if parent is None: root_id = node_id
        else: children_map.setdefault(parent, []).append(node_id)
    if root_id is None: return []
    messages = []
    def dfs(node_id):
        node = mapping.get(node_id, {})
        msg  = node.get("message")
        if msg:
            role    = msg.get("author", {}).get("role", "")
            content = msg.get("content", {})
            text    = extract_text(content).strip()
            ts      = msg.get("create_time")
            if text and role not in ("system",):
                messages.append((role, text, ts))
        for kid in children_map.get(node_id, []):
            dfs(kid)
    dfs(root_id)
    return messages

def project_for(conv: dict) -> str:
    """Return the ChatGPT project name, sanitized for filesystem use."""
    # ChatGPT exports may include project info under various keys
    project = (
        conv.get("project_name") or
        conv.get("project") or
        conv.get("workspace") or
        ""
    )
    if isinstance(project, dict):
        project = project.get("name") or project.get("title") or ""
    project = str(project).strip()
    return safe_dirname(project) if project else DEFAULT_PROJECT

def conv_to_md(conv: dict) -> str:
    title       = (conv.get("title") or "Untitled").strip()
    create_time = ts_to_iso(conv.get("create_time"))
    update_time = ts_to_iso(conv.get("update_time"))
    conv_id     = conv.get("id", "")
    project     = project_for(conv)

    lines = [
        "---",
        f'title: "{title.replace(chr(34), chr(39))}"',
        f"created: {create_time}",
        f"updated: {update_time}",
        f"source: chatgpt",
        f"project: {project}",
        f"chatgpt_id: {conv_id}",
        "tags:", "  - chatgpt", "---", "", f"# {title}", "",
    ]
    for role, text, ts in walk_messages(conv.get("mapping", {})):
        label  = ROLE_LABELS.get(role, f"**{role.capitalize()}**")
        ts_str = ts_to_iso(ts)
        lines += [f"### {label}" + (f" <small>{ts_str}</small>" if ts_str else ""), "", text, "", "---", ""]
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
    vault_dir = Path(vault_root) / ROOT / SERVICE
    vault_dir.mkdir(parents=True, exist_ok=True)

    handlers = [logging.FileHandler(vault_dir / LOG_FILE, encoding="utf-8")]
    if not quiet: handlers.append(logging.StreamHandler(sys.stdout))
    log = logging.getLogger("chatgpt")
    log.setLevel(logging.INFO)
    for h in handlers: log.addHandler(h)
    log.info("=== ChatGPT Sync started ===")

    export_p = Path(export_path).expanduser().resolve()
    tmpdir = None
    if export_p.suffix.lower() == ".zip":
        tmpdir = tempfile.mkdtemp(prefix="chatgpt_sync_")
        with zipfile.ZipFile(export_p, "r") as zf: zf.extractall(tmpdir)
        export_dir = Path(tmpdir)
    elif export_p.is_dir(): export_dir = export_p
    else:
        log.error(f"Not a ZIP or directory: {export_p}"); return {"new":0,"updated":0,"skipped":0,"errors":1}

    conv_file = export_dir / "conversations.json"
    if not conv_file.exists():
        candidates = list(export_dir.rglob("conversations.json"))
        if not candidates:
            log.error("conversations.json not found."); return {"new":0,"updated":0,"skipped":0,"errors":1}
        conv_file  = candidates[0]
        export_dir = conv_file.parent

    with open(conv_file, encoding="utf-8") as f: conversations = json.load(f)
    log.info(f"  {len(conversations)} conversations in export")

    state = load_state(vault_dir) if not force else {"last_run": None, "conversations": {}}
    known = state.get("conversations", {})
    counts = {"new": 0, "updated": 0, "skipped": 0, "errors": 0}

    for conv in conversations:
        conv_id     = conv.get("id", "")
        title       = (conv.get("title") or "Untitled").strip()
        update_time = conv.get("update_time")
        create_time = conv.get("create_time")
        project     = project_for(conv)

        try:
            prev = known.get(conv_id)
            if not force and prev:
                if prev.get("update_time") and update_time and float(update_time) <= float(prev["update_time"]):
                    counts["skipped"] += 1; continue

            out_dir = vault_dir / project
            if not dry_run: out_dir.mkdir(parents=True, exist_ok=True)

            date_prefix = ""
            if create_time:
                try: date_prefix = datetime.fromtimestamp(float(create_time), tz=timezone.utc).strftime("%Y-%m-%d") + " "
                except: pass

            stem = safe_filename(f"{date_prefix}{title}")
            if prev and prev.get("filename"):
                out_path = vault_dir / prev.get("project", project) / prev["filename"]
            else:
                out_path = unique_path(out_dir, stem)

            if not dry_run: out_path.write_text(conv_to_md(conv), encoding="utf-8")
            action = "updated" if prev else "new"
            counts[action] += 1
            log.info(f"  [{action.upper():7}]  {project}/{out_path.name}")
            known[conv_id] = {"update_time": update_time, "project": project, "filename": out_path.name}
        except Exception as e:
            counts["errors"] += 1; log.error(f"  [ERROR]    {title!r}: {e}")

    # Attachments
    att_dir = vault_dir / ATT_SUBDIR
    att_count = 0
    for f in export_dir.iterdir():
        if f.suffix.lower() in (".json", ".zip") or not f.is_file(): continue
        dest = att_dir / f.name
        if not dest.exists():
            if not dry_run: att_dir.mkdir(parents=True, exist_ok=True); shutil.copy2(f, dest)
            att_count += 1
    if att_count: log.info(f"  [ATTACH]   {att_count} new attachment(s)")

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
