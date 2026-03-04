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

You should see:
```
✅ API Key: CONFIGURED  (AIzaSyDi...xxxx)
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

## Files

| File | Purpose |
|------|---------|
| `gemini.py` | Main analyzer — calls Gemini AI on extracted log issues |
| `extract.py` | Log parser — extracts warnings/errors by category and severity |
| `config.yml` | AI model settings and phase-specific prompts |

## What Gets Sent to the AI

Each phase sends different files to give the AI full context:

| Data | syn | sim_rtl | par |
|------|-----|---------|-----|
| Primary config | `syn.yml` | `sim-rtl.yml` | `par.yml` |
| Auxiliary configs | `common.yml`, `sky130.yml` | `common.yml` | `common.yml`, `syn.yml`, `sky130.yml` |
| HDL source files | ✅ | ✅ | ✅ |
| Testbench files | — | ✅ (`*_tb.v`) | — |
| Timing report | `.setup_view.rpt` | — | postRoute reports |
| Log issues | Genus log | Sim log | Innovus log |

Files are sent **raw** (no comment stripping) so the AI can generate accurate diffs.

## Output & Logs

### Console
Markdown-formatted analysis rendered via Rich (if installed), otherwise plain text.

### Session Logs (shared, tamper-protected)
```
autoTA/logs/autoTA_<user>_<phase>_<timestamp>.json
```
JSON archives sealed with read-only permissions. Contains the full AI response, extracted issues, source code, and timing report.

### Legacy Text Logs (design-local)
```
<build-dir>/autota_logs/autoTA_<logfile>
```
Human-readable text log in the design build directory.

### Patch Archives (design-local)
```
<build-dir>/autota_patches/YYYY-MM-DD_HHMMSS_<phase>/
├── manifest.json      # timestamp, phase, git commit, list of backed-up files
├── ai_analysis.md     # full AI response
└── originals/         # backup copies of all source + config files
```
Created every run. Before any AI-suggested patch is applied, the originals are preserved here. Each archive folder is named by timestamp and phase for easy lookup.

**Finding what you need:**
- **Latest analysis?** → Sort `autota_patches/` by name (newest is last)
- **What files were involved?** → Read `manifest.json`
- **What did the AI say?** → Read `ai_analysis.md`
- **Need to rollback?** → Copy files from `originals/` back to their original paths (listed in `manifest.json`)