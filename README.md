# ai-vault-sync

Incremental sync of AI conversation exports into a structured Obsidian vault.

Supports **ChatGPT**, **DeepSeek**, **Gemini**, **Grok**, and **Perplexity**. On each run, only new or updated conversations are written — unchanged ones are skipped. State is tracked per service in a `.sync_state.json` file stored in your vault.

## Vault structure

```
vault/
└── AI-Chats/
    ├── ChatGPT/
    │   ├── general/                ← chats without a project
    │   └── My Project Name/        ← ChatGPT project folders
    ├── DeepSeek/
    │   ├── general/                ← no model assigned
    │   ├── deepseek-chat/
    │   └── deepseek-r1/
    ├── Gemini/
    │   └── general/
    ├── Grok/
    │   └── general/
    └── Perplexity/
        ├── general/                ← chats without a space
        └── Research Projects/      ← Perplexity space name
```

Every file gets YAML frontmatter with `created`, `updated`, `source`, and `tags` — ready for Dataview queries across all services.

## Quick start

```bash
# 1. Clone
git clone https://github.com/jeffnem/ai-vault-sync.git ~/scripts/ai-vault-sync

# 2. Copy and edit config
cp ~/scripts/ai-vault-sync/ai_vault_sync_config.json ~/.ai_vault_sync.json
# Edit ~/.ai_vault_sync.json with your vault path and export file locations

# 3. Run
python3 ~/scripts/ai-vault-sync/ai_vault_sync.py --config ~/.ai_vault_sync.json
```

No dependencies beyond Python 3.8+.

## Getting your exports

| Service | Where | Format |
|---|---|---|
| ChatGPT | [privacy.openai.com](https://privacy.openai.com) → Download my data | ZIP |
| DeepSeek | deepseek.com → Settings → Privacy → Export Data | ZIP |
| Gemini | [takeout.google.com](https://takeout.google.com) → My Activity → Gemini Apps → **JSON** | ZIP |
| Grok | [accounts.x.ai/data](https://accounts.x.ai/data) → Download account data | ZIP |
| Perplexity | [Perplexity to Obsidian](https://chatgpt2notion.com/products/chatgpt-to-obsidian/) Chrome extension → drop .md files into inbox folder | .md files |

> **Gemini note:** In Google Takeout, click "Multiple formats" next to My Activity and switch from HTML to **JSON**. HTML is not parseable.

## Config

```json
{
  "vault": "/Users/you/vault",
  "services": {
    "chatgpt":    { "export": "/Users/you/Downloads/chatgpt-export.zip" },
    "deepseek":   { "export": "/Users/you/Downloads/deepseek-export.zip" },
    "gemini":     { "export": "/Users/you/Downloads/gemini-takeout.zip" },
    "grok":       { "export": "/Users/you/Downloads/grok-export.zip" },
    "perplexity": { "inbox":  "/Users/you/Downloads/perplexity-inbox" }
  }
}
```

Omit any service you don't use.

## CLI options

```bash
# Sync all services
python3 ai_vault_sync.py --config ~/.ai_vault_sync.json

# Specific services only
python3 ai_vault_sync.py --config ~/.ai_vault_sync.json --only chatgpt deepseek

# See what would change without writing anything
python3 ai_vault_sync.py --config ~/.ai_vault_sync.json --dry-run

# Force full re-sync (ignores state)
python3 ai_vault_sync.py --config ~/.ai_vault_sync.json --force
```

## Scheduling (macOS)

Edit `com.jeffnemecek.ai-vault-sync.plist` to replace `YOURUSERNAME` with your macOS username and verify the python3 path (`which python3`), then:

```bash
cp com.jeffnemecek.ai-vault-sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.jeffnemecek.ai-vault-sync.plist

# Test immediately
launchctl start com.jeffnemecek.ai-vault-sync
```

Runs daily at 8:00 AM by default. Adjust `Hour` and `Minute` in the plist to change the schedule.

## Files

| File | Purpose |
|---|---|
| `ai_vault_sync.py` | Master runner |
| `chatgpt_sync.py` | ChatGPT converter |
| `deepseek_sync.py` | DeepSeek converter |
| `gemini_sync.py` | Gemini Takeout converter |
| `grok_sync.py` | Grok converter (defensive parser — schema undocumented by xAI) |
| `perplexity_sync.py` | Perplexity .md inbox ingester |
| `ai_vault_sync_config.json` | Config template |
| `com.jeffnemecek.ai-vault-sync.plist` | macOS launchd scheduler |

## Notes

- **Grok**: xAI hasn't published their export schema. The parser handles the most common observed patterns. If you get 0 conversations parsed, open an issue with a redacted sample of your export JSON.
- **Perplexity**: No bulk export exists. The Chrome extension saves one conversation at a time as .md files. Drop them in your configured inbox folder and the sync script handles the rest.
- Each service maintains its own `.sync_state.json` in its vault subfolder. Move your vault? Run `--force` once to rebuild state.
