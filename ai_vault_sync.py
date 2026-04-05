#!/usr/bin/env python3
"""
ai_vault_sync.py — Master runner for all AI service vault syncs.

Runs whichever services are configured (via config file or CLI flags).
Designed to be called by launchd on a schedule, or manually.

Usage:
    # Run all configured services
    python3 ai_vault_sync.py --config ~/.ai_vault_sync.json

    # Run specific services only
    python3 ai_vault_sync.py --config ~/.ai_vault_sync.json --only chatgpt deepseek

    # Dry run across all services
    python3 ai_vault_sync.py --config ~/.ai_vault_sync.json --dry-run

    # Force full re-sync for all services
    python3 ai_vault_sync.py --config ~/.ai_vault_sync.json --force

Config file (~/.ai_vault_sync.json):
{
  "vault": "/Users/YOURUSERNAME/vault",
  "services": {
    "chatgpt":    { "export": "/Users/YOURUSERNAME/Downloads/chatgpt-export.zip" },
    "deepseek":   { "export": "/Users/YOURUSERNAME/Downloads/deepseek-export.zip" },
    "gemini":     { "export": "/Users/YOURUSERNAME/Downloads/gemini-takeout.zip" },
    "grok":       { "export": "/Users/YOURUSERNAME/Downloads/grok-export.zip" },
    "perplexity": { "inbox": "/Users/YOURUSERNAME/Downloads/perplexity-inbox" }
  }
}

Omit any service from "services" to skip it entirely.
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Services are imported lazily so missing optional deps don't break everything
SERVICES = ["chatgpt", "deepseek", "gemini", "grok", "perplexity"]


def load_config(path: str) -> dict:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        print(f"Config file not found: {p}", file=sys.stderr)
        print("Create it with the template from the README, then re-run.", file=sys.stderr)
        sys.exit(1)
    with open(p) as f:
        return json.load(f)


def run_chatgpt(cfg: dict, vault: str, quiet: bool, force: bool, dry_run: bool) -> dict:
    export = cfg.get("export", "")
    if not export or not Path(export).expanduser().exists():
        print(f"  [ChatGPT]  Export not found: {export} — skipping")
        return {"new": 0, "updated": 0, "skipped": 0, "errors": 0}
    # Import the existing chatgpt_sync module (sibling file)
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    import chatgpt_sync
    return chatgpt_sync.sync(export, vault, quiet=quiet, force=force, dry_run=dry_run)


def run_deepseek(cfg: dict, vault: str, quiet: bool, force: bool, dry_run: bool) -> dict:
    export = cfg.get("export", "")
    if not export or not Path(export).expanduser().exists():
        print(f"  [DeepSeek] Export not found: {export} — skipping")
        return {"new": 0, "updated": 0, "skipped": 0, "errors": 0}
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    import deepseek_sync
    return deepseek_sync.sync(export, vault, quiet=quiet, force=force, dry_run=dry_run)


def run_gemini(cfg: dict, vault: str, quiet: bool, force: bool, dry_run: bool) -> dict:
    export = cfg.get("export", "")
    if not export or not Path(export).expanduser().exists():
        print(f"  [Gemini]   Export not found: {export} — skipping")
        return {"new": 0, "updated": 0, "skipped": 0, "errors": 0}
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    import gemini_sync
    return gemini_sync.sync(export, vault, quiet=quiet, force=force, dry_run=dry_run)


def run_grok(cfg: dict, vault: str, quiet: bool, force: bool, dry_run: bool) -> dict:
    export = cfg.get("export", "")
    if not export or not Path(export).expanduser().exists():
        print(f"  [Grok]     Export not found: {export} — skipping")
        return {"new": 0, "updated": 0, "skipped": 0, "errors": 0}
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    import grok_sync
    return grok_sync.sync(export, vault, quiet=quiet, force=force, dry_run=dry_run)


def run_perplexity(cfg: dict, vault: str, quiet: bool, force: bool, dry_run: bool) -> dict:
    inbox = cfg.get("inbox", "")
    if not inbox:
        print(f"  [Perplexity] No inbox path configured — skipping")
        return {"new": 0, "updated": 0, "skipped": 0, "errors": 0}
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    import perplexity_sync
    return perplexity_sync.sync(inbox, vault, quiet=quiet, force=force, dry_run=dry_run)


RUNNERS = {
    "chatgpt":    run_chatgpt,
    "deepseek":   run_deepseek,
    "gemini":     run_gemini,
    "grok":       run_grok,
    "perplexity": run_perplexity,
}


def main():
    parser = argparse.ArgumentParser(
        description="Sync all AI conversation exports to Obsidian vault.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default="~/.ai_vault_sync.json",
                        help="Path to config JSON file (default: ~/.ai_vault_sync.json)")
    parser.add_argument("--only",   nargs="+", choices=SERVICES,
                        help="Only run these services")
    parser.add_argument("--quiet",   action="store_true", help="Suppress stdout output")
    parser.add_argument("--force",   action="store_true", help="Force full re-sync")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    args = parser.parse_args()

    config   = load_config(args.config)
    vault    = str(Path(config["vault"]).expanduser().resolve())
    services = config.get("services", {})
    to_run   = args.only or list(services.keys())

    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n{'='*50}")
    print(f"  AI Vault Sync  —  {now}")
    if args.dry_run: print("  DRY RUN — nothing will be written")
    print(f"  Vault: {vault}")
    print(f"  Services: {', '.join(to_run)}")
    print(f"{'='*50}\n")

    totals = {"new": 0, "updated": 0, "skipped": 0, "errors": 0}

    for service in to_run:
        if service not in services:
            print(f"  [{service}] Not configured — skipping\n")
            continue
        runner = RUNNERS.get(service)
        if not runner:
            print(f"  [{service}] No runner available — skipping\n")
            continue

        print(f"── {service.upper()} ──")
        try:
            result = runner(services[service], vault,
                            quiet=args.quiet, force=args.force, dry_run=args.dry_run)
            for k in totals: totals[k] += result.get(k, 0)
            print(f"  ✅  {result.get('new',0)} new  |  {result.get('updated',0)} updated  |  "
                  f"{result.get('skipped',0)} skipped  |  {result.get('errors',0)} errors\n")
        except Exception as e:
            totals["errors"] += 1
            print(f"  ❌  {service} failed: {e}\n")

    print(f"{'='*50}")
    print(f"  TOTAL  {totals['new']} new  |  {totals['updated']} updated  |  "
          f"{totals['skipped']} skipped  |  {totals['errors']} errors")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
