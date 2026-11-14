# AnySearch Skill

Unified real-time search engine skill for AI agents. Supports general web search, vertical domain search, parallel batch search, and full-page content extraction.

## Download & Install

### For AI Agents

If your agent platform supports a skill marketplace/store, search for **anysearch** and install from there. Otherwise, download and install manually:

```bash
# Download (pin to a specific release version)
curl -L -o anysearch-skill.zip https://github.com/anysearch-ai/anysearch-skill/archive/refs/heads/main.zip
# or: wget -O anysearch-skill.zip https://github.com/anysearch-ai/anysearch-skill/archive/refs/heads/main.zip

# Verify integrity (replace with actual checksum from the release page)
# sha256sum anysearch-skill.zip

# Unzip
unzip anysearch-skill.zip

# Move to your agent's skill directory (example paths below, adjust for your agent)
# Claude Code:    mv anysearch-skill ~/.claude/skills/anysearch
# OpenCode:       mv anysearch-skill ~/.config/opencode/skills/anysearch
# Cursor/Windsurf: mv anysearch-skill <project>/.skills/anysearch
# Generic:        mv anysearch-skill <your_agent_skill_dir>/anysearch
# Shared agents:  mv anysearch-skill ~/.agents/skills/anysearch
```

`~/.agents/skills/` is a useful shared install location when multiple AI tools read from the same skill directory, including Codex, Cursor, and OpenClaw personal agent skills.

### For Humans

1. Download the zip: https://github.com/anysearch-ai/anysearch-skill/archive/refs/heads/main.zip
2. Unzip to your agent's skill directory
3. Configure API key (see below)
4. Run the entry test to verify installation

## API Key Configuration

Missing option values and MCP tool errors exit with a nonzero status and write
the error to stderr. A successful HTTP response can still contain a tool error;
check the CLI exit status before using its output.

An API key is **optional but strongly recommended**. Without a key, you can still use all search features via anonymous access, but with **lower rate limits and quota**.

### How to configure

Copy the example env file and fill in your key:

```bash
cp .env.example .env
# Edit .env and set: ANYSEARCH_API_KEY=<your_api_key_here>
```

Or set the environment variable directly:

```bash
export ANYSEARCH_API_KEY=<your_api_key_here>   # Linux/macOS
set ANYSEARCH_API_KEY=<your_api_key_here>       # Windows CMD
$env:ANYSEARCH_API_KEY="<your_api_key_here>"    # Windows PowerShell
```

### Get an API Key

Visit https://anysearch.com/console/api-keys to sign up and create a free API key.

Key priority order: `--api_key` CLI flag > `.env` file > environment variable > anonymous

## Post-Install Verification

After installation, probe the platform and run the entry test:

### Step 1: Detect available runtime

Run these checks in order. The first success determines the active CLI:

```bash
# Check Python (recommended)
python --version   # Need >= 3.6, requires `requests` library
python3 --version  # Need >= 3.6, requires `requests` library
# Check Node.js (alternative)
node --version     # Need >= 12, no external dependencies
# Check Shell (fallback)
# Windows: PowerShell 5.1+ / Linux/macOS: bash 4+
```

Priority: **Python > Node.js > Shell**

Important: do not assume `python` exists. On many macOS systems, the correct executable is `python3`. Check both `python` and `python3`; if either works, Python is available.

### Step 2: Run entry test (probe all available runtimes)

Run the `doc` command with **each available** runtime to verify the skill works, and observe which runs without errors or warnings:

```bash
# Python
python <skill_dir>/scripts/anysearch_cli.py doc

# Python 3 fallback (common on macOS)
python3 <skill_dir>/scripts/anysearch_cli.py doc

# Node.js (if available)
node <skill_dir>/scripts/anysearch_cli.js doc

# PowerShell (Windows)
powershell -ExecutionPolicy Bypass -File <skill_dir>/scripts/anysearch_cli.ps1 doc

# Bash (Linux/macOS)
bash <skill_dir>/scripts/anysearch_cli.sh doc
```

For each runtime, record whether the command succeeded and any errors/warnings encountered. The runtime that completes without errors and produces the cleanest output is the recommended runtime for this environment.

### Step 3: Persist recommended runtime to configuration file

Based on the entry test results, write the recommended runtime to `<skill_dir>/runtime.conf`:

```bash
echo "Runtime: <RUNTIME>" > <skill_dir>/runtime.conf
echo "Command: <COMMAND>" >> <skill_dir>/runtime.conf
```

Example for Python:

```bash
echo "Runtime: Python" > <skill_dir>/runtime.conf
echo "Command: python <skill_dir>/scripts/anysearch_cli.py" >> <skill_dir>/runtime.conf
```

Example for Python 3:

```bash
echo "Runtime: Python" > <skill_dir>/runtime.conf
echo "Command: python3 <skill_dir>/scripts/anysearch_cli.py" >> <skill_dir>/runtime.conf
```

Example for Node.js:

```bash
echo "Runtime: Node.js" > <skill_dir>/runtime.conf
echo "Command: node <skill_dir>/scripts/anysearch_cli.js" >> <skill_dir>/runtime.conf
```

Example for PowerShell:

```bash
echo "Runtime: PowerShell" > <skill_dir>/runtime.conf
echo "Command: powershell -ExecutionPolicy Bypass -File <skill_dir>/scripts/anysearch_cli.ps1" >> <skill_dir>/runtime.conf
```

Example for Bash:

```bash
echo "Runtime: Bash" > <skill_dir>/runtime.conf
echo "Command: bash <skill_dir>/scripts/anysearch_cli.sh" >> <skill_dir>/runtime.conf
```

**Important:** Runtime preferences are stored in `runtime.conf`, NOT in SKILL.md. The agent reads `runtime.conf` on skill load to determine the active CLI. If the file is missing or corrupted, the agent falls back to the Platform Detection procedure in SKILL.md. If `runtime.conf` already exists, replace it instead of appending.

### Routine agent usage

After `runtime.conf` exists, agents should use the stored `Command` directly for routine calls instead of running `doc` before every search. For example, if `runtime.conf` contains `Command: python3 <skill_dir>/scripts/anysearch_cli.py`, use:

```bash
python3 <skill_dir>/scripts/anysearch_cli.py search "query" --max_results 5
python3 <skill_dir>/scripts/anysearch_cli.py batch_search --queries '[{"query":"q1","max_results":5},{"query":"q2","max_results":5}]'
python3 <skill_dir>/scripts/anysearch_cli.py extract "https://example.com/page"
python3 <skill_dir>/scripts/anysearch_cli.py extract --url "https://example.com/page"
```

`extract` output is already Markdown. Do not pass `--format markdown`, `--format json`, or `--markdown`; the extract command only accepts the URL positional argument or `--url`/`-u`. If a subcommand argument is unclear or fails, run `<command> <subcommand> --help` for that subcommand rather than the full `doc` command.

### Step 4 (optional): Test a real search

```bash
python <skill_dir>/scripts/anysearch_cli.py search "hello world" --max_results 1
```

If your system does not provide `python`, use:

```bash
python3 <skill_dir>/scripts/anysearch_cli.py search "hello world" --max_results 1
```

A successful JSON response confirms the API connection is working.

## File Structure

```
anysearch/
├── .env.example              # API key configuration template
├── .env                      # Your API key (gitignored, create from .env.example)
├── runtime.conf              # Detected runtime preferences (gitignored)
├── runtime.conf.example      # Runtime configuration template
├── SKILL.md                  # Skill definition for AI agents
├── README.md                 # This file
└── scripts/
    ├── anysearch_cli.py       # Python CLI
    ├── anysearch_cli.js       # Node.js CLI
    ├── anysearch_cli.ps1      # PowerShell CLI
    └── anysearch_cli.sh       # Bash CLI
```
