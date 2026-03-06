# AutoTA+ — AI-Powered VLSI Log Analyzer

Automated debug agent for the Sledgehammer ASIC pipeline. Analyzes synthesis (Genus), RTL simulation, and place-and-route (Innovus) logs using Gemini AI.

## Setup

### 1. Get a Gemini API Key

Go to [Google Cloud API Credentials](https://console.cloud.google.com/apis/credentials) and create an API key with Gemini API access.

### 2. Save the Key

```bash
cd chipyard/vlsi/hammer/hammer/shell/autoTA
python3 gemini.py --set-key YOUR_API_KEY
source ~/.bashrc
```

### 3. Verify

```bash
python3 gemini.py --show-key
```

## Usage

```bash
# Analyze synthesis logs
python3 gemini.py --phase syn

# Analyze RTL simulation logs
python3 gemini.py --phase sim_rtl

# Analyze place-and-route logs
python3 gemini.py --phase par

# Analyze a specific log file
python3 gemini.py --phase syn genus.log2

# Help
python3 gemini.py --help
```

Run from your **design build directory** (e.g., `build-sky130-cm/gcd/`), or the DAG will set the working directory automatically.

## What Gets Sent to the AI

Every stage receives **all design config files** so the AI can predict downstream issues (e.g., syn_debug sees par.yml to forecast PAR problems).

| Data | syn | sim_rtl | par |
|------|-----|---------|-----|
| Primary config | `syn.yml` | `sim-rtl.yml` | `par.yml` |
| All other configs | ✅ every `.yml` in configs-design | ✅ | ✅ |
| HDL source files | ✅ (raw, no comment stripping) | ✅ | ✅ |
| Testbench files | — | ✅ (`*_tb.v`) | — |
| Timing report | `.setup_view.rpt` | — | postRoute reports |
| Log issues | Genus log | Sim log | Innovus log |

## AI Output Format

The AI outputs a structured analysis with:
1. **SUMMARY** — What happened, what's broken
2. **CURRENT ERRORS** — Each issue with file, line, evidence
3. **DOWNSTREAM PREDICTIONS** — What will fail in future stages
4. **PATCH** — A unified `git diff` that fixes all issues
5. **CHANGE LOG** — One bullet per file explaining the change
6. **ACTION** — PROCEED, PATCH_AND_RETRY, or ABORT

## Files

| File | Purpose |
|------|---------|
| `gemini.py` | Main analyzer — calls Gemini AI on extracted log issues |
| `extract.py` | Log parser — extracts warnings/errors by category and severity |
| `config.yml` | AI model settings and phase-specific prompts |

## Output & Logs

| Location | What | Persists |
|----------|------|----------|
| `autoTA/logs/*.json` | Tamper-proof JSON audit log (sealed read-only) | Shared |
| `<build-dir>/autota_logs/` | Legacy text logs | Per-design |
| `<build-dir>/autota_patches/<timestamp>_<phase>/` | Patch archives (see below) | Per-design |

### Patch Archives

Created every run. Contains backups of all files before any patching:

```
autota_patches/2026-03-04_132800_syn/
├── manifest.json      # timestamp, phase, git commit, backed-up file paths
├── ai_analysis.md     # full AI response (with the git diff)
└── originals/         # backup copies of all source + config files
```

**Finding what you need:**
- **Latest?** → Sort by folder name (newest last)
- **What files?** → `manifest.json`
- **What did the AI say?** → `ai_analysis.md`
- **Rollback?** → Copy files from `originals/` back (paths in `manifest.json`)