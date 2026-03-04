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

## Output

- **Console:** Markdown-formatted analysis (rendered via Rich if available)
- **`autota_logs/`:** Text session logs in the design build directory
- **`logs/`:** JSON audit archives (tamper-protected) in the autoTA directory