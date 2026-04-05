#!/usr/bin/env python3
"""
perplexity_sync.py — Ingest Perplexity .md files into Obsidian vault.

Perplexity has no bulk export. The "Perplexity to Obsidian" Chrome extension
(chatgpt2notion.com) saves individual conversations as .md files with YAML
frontmatter including a `space:` field (Perplexity's project/space name).

Workflow:
  1. Export conversations from Perplexity using the browser extension
  2. Drop the .md files into your "Perplexity inbox" folder
     (default: ~/Downloads/perplexity-inbox/)
  3. Run this script — it normalizes frontmatter, moves files into the vault
     under the correct space subfolder, and tracks state

Vault layout:
  <vault>/Perplexity/
    conversations/          ← no space assigned
    <Space Name>/           ← from frontmatter `space:` field
    .sync_state.json
    sync.log

The script is idempotent — re-dropping the same file is safe.
"""

import json, logging, re, sys
from datetime import datetime, timezone
from pathlib import Path

SERVICE    = "Perplexity"
STATE_FILE = ".sync_state.json"
LOG_FILE   = "sync.log"
DEFAULT_SUBDIR = "general"

REQUIRED_FRONTMATTER_KEYS = {"title", "source"}


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """
    Parse YAML frontmatter from a markdown file.
    Returns (frontmatter_dict, body_text).
    Handles files with or without frontmatter.
    """
    if not text.startswith("---"):
        return {}, text

    end = text.find("\n---", 3)
    if end == -1:
        return {}, text

    fm_text = text[3:end].strip()
    body    = text[end + 4:].lstrip("\n")
    fm = {}
    for line in fm_text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm, body

def normalize_md(text: str, source_path: Path) -> tuple[str, dict]:
    """
    Normalize a Perplexity .md file:
    - Ensure YAML frontmatter has source: perplexity
    - Add created/updated timestamps if missing
    - Return (normalized_text, frontmatter_dict)
    """
    fm, body = parse_frontmatter(text)

    # Fill in missing fields
    fm.setdefault("source", "perplexity")
    fm.setdefault("title",  source_path.stem)
    if "created" not in fm:
        try:
            mtime = source_path.stat().st_mtime
            fm["created"] = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except: pass
    fm.setdefault("updated", fm.get("created", ""))

    # Ensure tags include perplexity
    tags_line = "  - perplexity"
    fm_lines = ["---"]
    for k, v in fm.items():
        if k == "tags": continue
        fm_lines.append(f'{k}: "{v}"' if " " in v or ":" in v else f"{k}: {v}")
    fm_lines += ["tags:", "  - perplexity", "---"]

    normalized = "\n".join(fm_lines) + "\n\n" + body
    return normalized, fm

def subdir_for(fm: dict) -> str:
    """Determine vault subfolder from frontmatter space field."""
    space = fm.get("space") or fm.get("Space") or fm.get("project") or ""
    space = space.strip().strip('"').strip("'")
    if space:
        # Sanitize for filesystem
        return re.sub(r'[\\/:*?"<>|]', "", space).strip() or DEFAULT_SUBDIR
    return DEFAULT_SUBDIR

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

def load_state(d: Path) -> dict:
    p = d / STATE_FILE
    if p.exists():
        try:
            with open(p) as f: return json.load(f)
        except: pass
    return {"last_run": None, "files": {}}

def save_state(d: Path, state: dict, dry_run: bool):
    if dry_run: return
    state["last_run"] = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(d / STATE_FILE, "w") as f: json.dump(state, f, indent=2)

def sync(inbox_path: str, vault_root: str, quiet=False, force=False, dry_run=False):
    vault_dir = Path(vault_root) / "AI-Chats" / SERVICE
    vault_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir = Path(inbox_path).expanduser().resolve()

    handlers = [logging.FileHandler(vault_dir / LOG_FILE, encoding="utf-8")]
    if not quiet: handlers.append(logging.StreamHandler(sys.stdout))
    log = logging.getLogger("perplexity")
    log.setLevel(logging.INFO)
    for h in handlers: log.addHandler(h)
    log.info("=== Perplexity Sync started ===")

    if not inbox_dir.exists():
        log.info(f"  Inbox directory does not exist: {inbox_dir} — nothing to do")
        return {"new": 0, "updated": 0, "skipped": 0, "errors": 0}

    md_files = list(inbox_dir.glob("*.md"))
    log.info(f"  {len(md_files)} .md files in inbox")

    state = load_state(vault_dir) if not force else {"last_run": None, "files": {}}
    known = state.get("files", {})
    counts = {"new": 0, "updated": 0, "skipped": 0, "errors": 0}

    for src in md_files:
        file_key = src.name
        try:
            text = src.read_text(encoding="utf-8")
            # Use file hash to detect changes
            import hashlib
            file_hash = hashlib.md5(text.encode()).hexdigest()

            prev = known.get(file_key)
            if not force and prev and prev.get("hash") == file_hash:
                counts["skipped"] += 1; continue

            normalized, fm = normalize_md(text, src)
            subdir         = subdir_for(fm)
            title          = fm.get("title", src.stem)

            # Date prefix from created field
            date_prefix = ""
            created = fm.get("created", "")
            if created:
                try:
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    date_prefix = dt.strftime("%Y-%m-%d") + " "
                except: pass

            out_dir = vault_dir / subdir
            if not dry_run: out_dir.mkdir(parents=True, exist_ok=True)

            stem = safe_filename(f"{date_prefix}{title}")
            if prev and prev.get("filename") and prev.get("subdir"):
                out_path = vault_dir / prev["subdir"] / prev["filename"]
            else:
                out_path = unique_path(out_dir, stem)

            if not dry_run:
                out_path.write_text(normalized, encoding="utf-8")
                src.unlink()  # Remove from inbox once processed

            action = "updated" if prev else "new"
            counts[action] += 1
            log.info(f"  [{action.upper():7}]  {subdir}/{out_path.name}")
            known[file_key] = {"hash": file_hash, "subdir": subdir, "filename": out_path.name}
        except Exception as e:
            counts["errors"] += 1; log.error(f"  [ERROR]    {file_key}: {e}")

    state["files"] = known
    save_state(vault_dir, state, dry_run)
    log.info(f"=== Done: {counts['new']} new, {counts['updated']} updated, {counts['skipped']} skipped ===")
    return counts

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Ingest Perplexity .md exports into Obsidian vault")
    p.add_argument("inbox", help="Folder where you drop Perplexity .md files")
    p.add_argument("vault", help="Vault root directory")
    p.add_argument("--quiet", action="store_true"); p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    r = sync(args.inbox, args.vault, args.quiet, args.force, args.dry_run)
    print(f"\n✅  {r['new']} new  |  {r['updated']} updated  |  {r['skipped']} skipped  |  {r['errors']} errors")
