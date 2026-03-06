#!/usr/bin/env python3
import sys
import os
import subprocess
import shutil
import gzip
import json
import time
from datetime import datetime

# ==========================================
# SCRIPT NAME & EARLY COMMANDS
# ==========================================
SCRIPT_NAME = os.path.basename(__file__)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def get_bashrc_path() -> str:
    return os.path.expanduser("~/.bashrc")


def print_help() -> None:
    print("\n" + "=" * 60)
    print("AutoTA+ — AI-Powered VLSI Log Analyzer (Sledgehammer)")
    print("=" * 60)
    print("\nUSAGE:")
    print(f"   {SCRIPT_NAME} [--phase PHASE] [logfile]")
    print("\nPHASES:")
    print("   --phase syn       Synthesis logs (Genus)        [default]")
    print("   --phase sim_rtl   RTL simulation logs")
    print("   --phase par       Place-and-route logs (Innovus)")
    print("\nAPI KEY MANAGEMENT:")
    print("   --set-key KEY     Save Gemini API key to ~/.bashrc")
    print("   --show-key        Display current API key status")
    print("\nEXAMPLES:")
    print(f"   {SCRIPT_NAME} --set-key AIzaSyA...      # First-time setup")
    print("   source ~/.bashrc                         # Apply the key")
    print(f"   {SCRIPT_NAME} --phase syn               # Analyze latest syn log")
    print(f"   {SCRIPT_NAME} --phase par               # Analyze latest PAR log")
    print(f"   {SCRIPT_NAME} --phase syn genus.log2    # Analyze specific log")
    print("=" * 60 + "\n")


API_KEY_FILE = os.path.join(SCRIPT_DIR, ".api_key")


def set_api_key(api_key: str) -> None:
    # Write to local file (works in Airflow, shell, cron — anything)
    with open(API_KEY_FILE, "w") as f:
        f.write(api_key.strip())
    os.chmod(API_KEY_FILE, 0o600)  # owner-only read/write

    print("\n" + "=" * 60)
    print(f"✅ API key saved to {API_KEY_FILE}")
    print("   No 'source' needed — ready to use immediately.")
    print("=" * 60 + "\n")


def show_api_key() -> None:
    api_key = os.environ.get("AUTOTA_API_KEY")
    if not api_key and os.path.exists(API_KEY_FILE):
        with open(API_KEY_FILE) as f:
            api_key = f.read().strip()
    print("\n" + "=" * 60)
    if api_key:
        masked = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else api_key[:4] + "..."
        print(f"✅ API Key: CONFIGURED  ({masked})")
    else:
        print("❌ API Key: NOT SET")
        print(f"\nRun: {SCRIPT_NAME} --set-key YOUR_KEY")
    print("=" * 60 + "\n")


def get_api_key() -> str:
    # 1. Environment variable (highest priority)
    api_key = os.environ.get("AUTOTA_API_KEY")
    if api_key:
        return api_key

    # 2. Local key file (works in Airflow, cron, any process)
    if os.path.exists(API_KEY_FILE):
        try:
            with open(API_KEY_FILE) as f:
                val = f.read().strip()
            if val:
                return val
        except Exception:
            pass

    # 3. Bashrc fallback (legacy)
    bashrc = os.path.expanduser("~/.bashrc")
    if os.path.exists(bashrc):
        try:
            with open(bashrc, "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("export AUTOTA_API_KEY="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            return val
        except Exception:
            pass

    print("\n" + "=" * 60)
    print("❌ No API key configured.")
    print(f"\nRun: {SCRIPT_NAME} --set-key YOUR_KEY")
    print("Get a key from: https://aistudio.google.com/apikey")
    print("=" * 60 + "\n")
    sys.exit(1)


def _handle_early_commands() -> None:
    """Handle --help/--set-key/--show-key BEFORE conda bootstrap."""
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        print_help()
        sys.exit(0)
    if "--show-key" in argv:
        show_api_key()
        sys.exit(0)
    if "--set-key" in argv:
        idx = argv.index("--set-key")
        if idx + 1 >= len(argv):
            print(f"Usage: {SCRIPT_NAME} --set-key YOUR_API_KEY")
            sys.exit(1)
        set_api_key(argv[idx + 1])
        sys.exit(0)


_handle_early_commands()

# ==========================================
# AUTO-SETUP BLOCK (Conda bootstrap)
# ==========================================

def _pick_conda_executable() -> str:
    conda_exe = os.environ.get("AUTOTA_CONDA")
    if conda_exe and os.path.isfile(conda_exe) and os.access(conda_exe, os.X_OK):
        return conda_exe
    bundled = os.path.join(SCRIPT_DIR, "conda", "bin", "conda")
    if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
        return bundled
    return "conda"


try:
    from google import genai
    import yaml
    import glob
except ImportError:
    if os.environ.get("AUTOTA_REEXEC") == "1":
        print("Error: Auto-setup loop detected. Conda environment failed to load dependencies.")
        sys.exit(1)

    script_abs_path = os.path.abspath(__file__)
    script_args = " ".join(sys.argv[1:])
    conda_exe = _pick_conda_executable()

    cmd = (
        f"export AUTOTA_REEXEC=1; "
        f"eval \"$({conda_exe} shell.bash hook)\"; "
        "conda activate; "
        f"python3 \"{script_abs_path}\" {script_args}"
    )
    try:
        os.execv("/bin/bash", ["bash", "-c", cmd])
    except Exception as e:
        print(f"Failed to auto-setup environment: {e}")
        sys.exit(1)

# ==========================================
# POST-BOOTSTRAP IMPORTS
# ==========================================

try:
    from rich.console import Console
    from rich.markdown import Markdown
    RICH_AVAILABLE = True
    console = Console()
except ImportError:
    RICH_AVAILABLE = False

# ==========================================
# CONFIGURATION & GLOBALS
# ==========================================

CURRENT_LAB_DIR = os.getcwd()
ACTUAL_CONFIG_PATH = ""

import argparse
_parser = argparse.ArgumentParser(description="AutoTA+ AI log analyzer", add_help=False)
_parser.add_argument("--phase", default="syn", choices=["syn", "sim_rtl", "par"],
                     help="VLSI phase to analyze (default: syn)")
_parser.add_argument("logfile", nargs="?", default=None, help="Specific log file to analyze")
_args, _remaining = _parser.parse_known_args()
PHASE = _args.phase

PHASE_CONFIG = {
    "syn":     {"log_dir": "syn-rundir",  "log_glob": "genus.log*",   "config_name": "syn.yml",     "issues_file": "synthesis_issues.log", "prompt_key": "prompt"},
    "sim_rtl": {"log_dir": "sim-rundir",  "log_glob": "*.log",        "config_name": "sim-rtl.yml", "issues_file": "sim_issues.log",       "prompt_key": "sim_rtl_prompt"},
    "par":     {"log_dir": "par-rundir",  "log_glob": "innovus.log*", "config_name": "par.yml",     "issues_file": "par_issues.log",       "prompt_key": "par_prompt"},
}
CURRENT_PHASE = PHASE_CONFIG[PHASE]
ISSUES_FILE = os.path.join(CURRENT_LAB_DIR, CURRENT_PHASE["issues_file"])

# ==========================================
# CONFIG LOADING
# ==========================================


def load_autota_config():
    config_path = os.path.join(SCRIPT_DIR, "config.yml")
    try:
        with open(config_path, "r") as f:
            data = yaml.safe_load(f)
        if "autota" not in data:
            print(f" Error: 'autota' section missing in {config_path}")
            sys.exit(1)
        return data["autota"]
    except FileNotFoundError:
        print(f" Error: Could not find config.yml at {config_path}")
        sys.exit(1)
    except Exception as e:
        print(f" Error reading config.yml: {e}")
        sys.exit(1)


def load_lab_config():
    global ACTUAL_CONFIG_PATH
    config_name = CURRENT_PHASE["config_name"]
    abs_lab_dir = os.path.abspath(CURRENT_LAB_DIR)
    dir_parts = abs_lab_dir.split(os.sep)

    search_options = [config_name, f"../{config_name}", f"../../{config_name}"]
    if len(dir_parts) >= 2:
        for i in range(-1, -4, -1):
            design_guess = dir_parts[i]
            search_options.extend([
                f"../../configs-design/{design_guess}/{config_name}",
                f"../../../configs-design/{design_guess}/{config_name}",
                f"../../../../configs-design/{design_guess}/{config_name}",
                f"../../../../../configs-design/{design_guess}/{config_name}"
            ])

    for opt in search_options:
        config_path = os.path.join(CURRENT_LAB_DIR, opt)
        if os.path.exists(config_path):
            print(f" Using design config: {os.path.normpath(config_path)}")
            ACTUAL_CONFIG_PATH = os.path.abspath(config_path)
            try:
                with open(config_path, "r") as f:
                    return yaml.safe_load(f)
            except Exception as e:
                print(f" Error reading {config_path}: {e}")
                sys.exit(1)

    print(f" Warning: Could not find {config_name} relative to {CURRENT_LAB_DIR}")

    # Fallback: try common.yml if the phase-specific config doesn't exist yet
    if config_name != "common.yml":
        print(f" Trying fallback: common.yml")
        for opt in search_options:
            fallback = os.path.join(CURRENT_LAB_DIR, opt.replace(config_name, "common.yml"))
            if os.path.exists(fallback):
                print(f" Using fallback config: {os.path.normpath(fallback)}")
                ACTUAL_CONFIG_PATH = os.path.abspath(fallback)
                try:
                    with open(fallback, "r") as f:
                        return yaml.safe_load(f)
                except Exception as e:
                    print(f" Error reading {fallback}: {e}")
                    break

    print(" Proceeding without a design config file.")
    return {}


# Load configs and initialize client
autota_config = load_autota_config()
lab_config = load_lab_config()

api_key = get_api_key()
try:
    client = genai.Client(api_key=api_key)
except Exception as e:
    print(f" Error initializing Gemini Client: {e}")
    client = None

DEFAULT_LOG_DIR = CURRENT_PHASE["log_dir"]

# ==========================================
# SMART LOG FILE SELECTION
# ==========================================


def get_target_log_file():
    log_dir_relative = autota_config.get("synth_log_dir", DEFAULT_LOG_DIR)
    log_dir = os.path.join(CURRENT_LAB_DIR, log_dir_relative)

    if _args.logfile:
        full_path = os.path.join(log_dir, _args.logfile)
        if os.path.exists(full_path):
            return full_path
        print(f" Error: File '{_args.logfile}' not found in {log_dir_relative}")
        sys.exit(1)

    if not os.path.exists(log_dir):
        print(f" Error: Log directory not found at: {log_dir_relative}")
        sys.exit(1)

    log_glob = CURRENT_PHASE["log_glob"]
    files = glob.glob(os.path.join(log_dir, log_glob))
    if not files:
        print(f" Error: No '{log_glob}' files found in {log_dir_relative}")
        sys.exit(1)

    latest_file = max(files, key=os.path.getmtime)
    print(f" Using latest log: {os.path.basename(latest_file)}")
    return latest_file


target_log_path = get_target_log_file()
EXTRACT_SCRIPT_PATH = os.path.join(SCRIPT_DIR, "extract.py")
EXTRACT_CMD = ["python3", EXTRACT_SCRIPT_PATH, "--phase", PHASE, target_log_path]

# ==========================================
# TIMING REPORT ANALYSIS
# ==========================================


def _read_text_maybe_gz(path: str) -> str:
    """Read a text file that may be gzip-compressed."""
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rt", errors="replace") as f:
                return f.read()
        with open(path, "r", errors="replace") as f:
            return f.read()
    except Exception as e:
        return f"Error reading timing report {path}: {e}"


def _parse_timing_report_text(text: str) -> str:
    """Extract compact summary: WNS/TNS + top 2 critical paths."""
    try:
        lines = text.splitlines(keepends=True)
        summary = []
        critical_paths = []
        current = []
        in_summary = False
        in_path = False

        for line in lines:
            if re.search(r"Timing Summary|WNS|TNS|Slack", line):
                in_summary = True
            if in_summary and line.strip() == "":
                in_summary = False
            if in_summary:
                summary.append(line)

            if re.search(r"Startpoint:|Endpoint:|Path Group:|Critical Path", line):
                if current and len(critical_paths) < 2:
                    critical_paths.append("".join(current))
                current = [line]
                in_path = True
                continue

            if in_path:
                current.append(line)
                if line.strip() == "" or re.match(r"^-{5,}$", line.strip()):
                    in_path = False

        if current and len(critical_paths) < 2:
            critical_paths.append("".join(current))

        out = "=== TIMING SUMMARY (key metrics) ===\n"
        out += "".join(summary[:20]).strip() + "\n"
        out += "\n=== TOP 2 CRITICAL PATHS ===\n"
        out += "\n".join(critical_paths[:2]).strip()

        if len(out) > 3000:
            out = out[:3000] + "\n... [truncated for token efficiency]"
        return out.strip() if out.strip() else text[:2000]
    except Exception as e:
        return f"Error parsing timing report: {e}"


def _pick_best_par_timing_reports(timing_dir: str) -> list:
    """Prefer postRoute setup+hold reports; fallback to postCTS."""
    def latest(patterns):
        matches = []
        for p in patterns:
            matches.extend(glob.glob(os.path.join(timing_dir, p)))
        return max(matches, key=os.path.getmtime) if matches else None

    setup = latest(["*postRoute*all.tarpt.gz", "*postRoute*all*.tarpt.gz",
                     "*postRoute*all.tarpt", "*postRoute*all*.rpt.gz", "*postRoute*all*.rpt"])
    hold = latest(["*postRoute*all*hold.tarpt.gz", "*postRoute*all*hold*.tarpt.gz",
                    "*postRoute*all*hold.tarpt", "*postRoute*all*hold*.rpt.gz", "*postRoute*all*hold*.rpt"])

    if setup is None:
        setup = latest(["*postCTS*all.tarpt.gz", "*postCTS*all*.tarpt.gz",
                         "*postCTS*all.tarpt", "*postCTS*all*.rpt.gz", "*postCTS*all*.rpt"])
    if hold is None:
        hold = latest(["*postCTS*all*hold.tarpt.gz", "*postCTS*all*hold*.tarpt.gz",
                        "*postCTS*all*hold.tarpt", "*postCTS*all*hold*.rpt.gz", "*postCTS*all*hold*.rpt"])

    out = []
    if setup:
        out.append(setup)
    if hold and hold != setup:
        out.append(hold)
    return out[:2]


def get_timing_report_content():
    """Phase-aware timing report extraction."""
    if PHASE == "sim_rtl":
        return "No timing report for RTL simulation phase."

    if PHASE == "par":
        timing_dir = os.path.join(CURRENT_LAB_DIR, "par-rundir", "timingReports")
        if not os.path.exists(timing_dir):
            return "No timingReports directory found in par-rundir."

        chosen = _pick_best_par_timing_reports(timing_dir)
        if not chosen:
            candidates = glob.glob(os.path.join(timing_dir, "*.tarpt*")) + \
                         glob.glob(os.path.join(timing_dir, "*.rpt*"))
            if not candidates:
                return "No timing reports found in par-rundir/timingReports."
            chosen = [max(candidates, key=os.path.getmtime)]

        blocks = []
        for path in chosen:
            raw = _read_text_maybe_gz(path)
            blocks.append(f"=== PAR TIMING: {os.path.basename(path)} ===\n{_parse_timing_report_text(raw)}")
            print(f" Including PAR Timing: {os.path.basename(path)}")
        return "\n\n".join(blocks)

    # SYN timing reports
    log_dir_relative = autota_config.get("synth_log_dir", DEFAULT_LOG_DIR)
    reports_dir = os.path.join(CURRENT_LAB_DIR, log_dir_relative, "reports")
    if not os.path.exists(reports_dir):
        return "No reports directory found (synthesis might have failed early)."

    files = glob.glob(os.path.join(reports_dir, "*.setup_view.rpt"))
    if not files:
        files = glob.glob(os.path.join(reports_dir, "*timing*.rpt"))
    if not files:
        return "No timing report found."

    latest_report = max(files, key=os.path.getmtime)
    print(f" Including Timing Report: {os.path.basename(latest_report)}")
    try:
        with open(latest_report, "r") as f:
            raw = f.read()
        return _parse_timing_report_text(raw)
    except Exception as e:
        return f"Error reading timing report: {e}"


# ==========================================
# FILE RETRIEVAL & COMMENT STRIPPING
# ==========================================


def get_hdl_source_files():
    synth_inputs = lab_config.get("synthesis.inputs", {}) if lab_config else {}
    if "input_files" in synth_inputs:
        return synth_inputs["input_files"]
    print(" Warning: Could not find 'input_files' in synthesis.inputs.")
    return []


def get_auxiliary_config_files():
    """Find ALL YAML configs in the design config directory.

    Every stage gets every config file so the AI can predict
    downstream issues (e.g., syn_debug sees par.yml to forecast PAR problems).

    Returns a dict of {filename: content}.
    """
    config_dir = os.path.dirname(ACTUAL_CONFIG_PATH) if ACTUAL_CONFIG_PATH else None
    results = {}

    if not config_dir or not os.path.isdir(config_dir):
        return results

    for name in sorted(os.listdir(config_dir)):
        if not name.endswith((".yml", ".yaml")):
            continue
        path = os.path.join(config_dir, name)
        # Skip the primary config (already sent separately)
        if os.path.abspath(path) == os.path.abspath(ACTUAL_CONFIG_PATH):
            continue
        try:
            with open(path, "r") as f:
                content = f.read()
            results[name] = content
            print(f" Loaded config: {name}")
        except Exception:
            pass
    return results


def get_testbench_files():
    """Find testbench files for sim_rtl phase.

    Returns a dict of {filename: content}.
    """
    if PHASE != "sim_rtl":
        return {}

    results = {}
    # Check sim config for testbench info
    sim_inputs = lab_config.get("sim.inputs", {}) if lab_config else {}
    tb_name = sim_inputs.get("tb_name", "")

    # Search for testbench files
    tb_patterns = []
    if tb_name:
        tb_patterns.append(f"{tb_name}.v")
        tb_patterns.append(f"{tb_name}.sv")

    # Also search for any *_tb.v files in the source directory
    search_dirs = [
        CURRENT_LAB_DIR,
        os.path.join(CURRENT_LAB_DIR, "src"),
        os.path.join(CURRENT_LAB_DIR, "..", "..", "src"),
    ]

    for search_dir in search_dirs:
        if not os.path.isdir(search_dir):
            continue
        for f in os.listdir(search_dir):
            if f.endswith((".v", ".sv")) and ("_tb" in f or "tb_" in f or f in [p for p in tb_patterns]):
                fpath = os.path.join(search_dir, f)
                if f not in results:
                    try:
                        content = get_file_content_smart(fpath)
                        if content:
                            results[f] = content
                            print(f" Loaded testbench: {f}")
                    except Exception:
                        pass
    return results


def get_file_content_smart(filename, max_chars: int = 30000):
    """Locate file and truncate if needed. No comment stripping — AI needs
    raw content so it can generate accurate diffs."""
    search_paths = [
        os.path.join(CURRENT_LAB_DIR, filename),
        os.path.join(CURRENT_LAB_DIR, "src", filename),
        os.path.join(CURRENT_LAB_DIR, "..", "..", "src", filename),
        os.path.join(CURRENT_LAB_DIR, "..", "..", "..", "src", filename)
    ]

    raw_content = None
    for path in search_paths:
        if os.path.exists(path):
            with open(path, "r") as f:
                raw_content = f.read()
            break

    if raw_content is None:
        print(f" Warning: Could not find source file: {filename}")
        return None

    # Truncate keeping head + tail if too long
    if len(raw_content) > max_chars:
        half = max_chars // 2
        raw_content = (raw_content[:half] +
                       "\n\n... [middle section truncated] ...\n\n" +
                       raw_content[-half:])
    return raw_content


# ==========================================
# AI ANALYSIS
# ==========================================


def analyze_issues(synthesis_issues, hdl_code, timing_report_content,
                   aux_configs=None, testbench_code=None):
    settings = autota_config.get("ai_settings", {})
    model_name = settings.get("model", "gemini-3-flash-preview")
    prompt_key = CURRENT_PHASE["prompt_key"]
    prompt_text = settings.get(prompt_key, settings.get("prompt", "Analyze these issues."))

    # Read primary config (may not exist on first run)
    config_raw = "No primary config file found for this phase."
    config_label = "(no config)"
    if ACTUAL_CONFIG_PATH and os.path.exists(ACTUAL_CONFIG_PATH):
        config_label = os.path.basename(ACTUAL_CONFIG_PATH)
        try:
            with open(ACTUAL_CONFIG_PATH, "r") as f:
                config_raw = f.read()
        except Exception as e:
            config_raw = f"Error reading config: {e}"

    # Build auxiliary configs section
    aux_section = ""
    if aux_configs:
        for name, content in aux_configs.items():
            aux_section += f"\nAUXILIARY CONFIG ({name}):\n{content}\n"

    # Build testbench section
    tb_section = ""
    if testbench_code:
        for name, content in testbench_code.items():
            tb_section += f"\n//TESTBENCH: {name}\n{content}\n"

    full_prompt = (
        f"{prompt_text}\n"
        "---------------------------------------------------\n"
        f"CONFIG ({config_label}) CONTENT:\n"
        f"{config_raw}\n\n"
        f"{aux_section}"
        "LOG ISSUES:\n"
        f"{synthesis_issues}\n\n"
        "VERILOG SOURCE:\n"
        f"{hdl_code}\n\n"
        f"{tb_section}"
        "TIMING REPORT:\n"
        f"{timing_report_content}"
    )

    try:
        response = client.models.generate_content(model=model_name, contents=full_prompt)
        usage = getattr(response, "usage_metadata", None)
        if usage:
            prompt_tok = getattr(usage, "prompt_token_count", 0)
            output_tok = getattr(usage, "candidates_token_count", 0)
            total_tok = getattr(usage, "total_token_count", 0)
            print(f"\n[TOKEN USAGE]  Input: {prompt_tok:,}  Output: {output_tok:,}  Total: {total_tok:,}")
        return response.text
    except Exception as e:
        return f"API Error: {e}"


# ==========================================
# SESSION LOGGING & AUDIT
# ==========================================


def log_session(analysis, target_log_path, config_path, synthesis_issues,
                hdl_code, timing_report):
    """Write a tamper-resistant JSON session archive."""
    log_dir = os.path.join(SCRIPT_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    user = os.environ.get("USER", "unknown")

    # Git metadata
    try:
        git_user = subprocess.check_output(
            ["git", "config", "user.name"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        git_user = "not_configured"
    try:
        git_hash = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True,
            cwd=CURRENT_LAB_DIR, stderr=subprocess.DEVNULL).strip()
    except Exception:
        git_hash = "no_commit"

    session_data = {
        "metadata": {
            "user": user,
            "git_user": git_user,
            "git_commit": git_hash,
            "working_dir": CURRENT_LAB_DIR,
            "phase": PHASE.upper(),
            "log_file": os.path.basename(target_log_path),
            "config_file": os.path.basename(config_path) if config_path else "",
            "timestamp": timestamp,
        },
        "ai_response": analysis,
        "logs": {
            "issues_extracted": synthesis_issues,
            "timing_report": timing_report,
        },
        "source_code": hdl_code,
    }

    archive_filename = f"autoTA_{user}_{PHASE}_{timestamp}.json"
    archive_path = os.path.join(log_dir, archive_filename)

    try:
        fd = os.open(archive_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(session_data, f, indent=2)
        # Seal: read-only for group, nothing else
        os.chmod(archive_path, 0o440)
        print(f" Session sealed: logs/{archive_filename}")
    except FileExistsError:
        print(f" Warning: Session log already exists: {archive_filename}")
    except PermissionError:
        print(f" Warning: Permission denied writing to {log_dir}")
    except Exception as e:
        print(f" Warning: Could not save session log: {e}")

    # Also save the old-style persistent log for backwards compatibility
    _save_persistent_log(analysis, target_log_path, config_path,
                         synthesis_issues, hdl_code, timing_report)


def archive_patch_session(analysis, phase, source_files_used):
    """Create a timestamped archive with backups of files the AI may patch.

    Structure:
        autota_patches/
        └── YYYY-MM-DD_HHMMSS_phase/
            ├── manifest.json    # metadata, file list, AI diagnosis
            ├── ai_analysis.md   # full AI response
            └── originals/       # backup copies of all source + config files

    Returns the archive directory path (for Airflow log output).
    """
    patches_dir = os.path.join(CURRENT_LAB_DIR, "autota_patches")
    timestamp = time.strftime("%Y-%m-%d_%H%M%S")
    session_dir = os.path.join(patches_dir, f"{timestamp}_{phase}")
    originals_dir = os.path.join(session_dir, "originals")
    os.makedirs(originals_dir, exist_ok=True)

    # Git commit hash
    try:
        git_hash = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True,
            cwd=CURRENT_LAB_DIR, stderr=subprocess.DEVNULL).strip()
    except Exception:
        git_hash = "unknown"

    # Back up all source files the AI was given
    backed_up = []
    for filepath in source_files_used:
        abspath = os.path.abspath(filepath)
        if os.path.exists(abspath):
            dest = os.path.join(originals_dir, os.path.basename(abspath))
            try:
                shutil.copy2(abspath, dest)
                backed_up.append({
                    "original_path": abspath,
                    "backup": f"originals/{os.path.basename(abspath)}",
                })
            except Exception as e:
                print(f" Warning: Could not back up {abspath}: {e}")

    # Save AI analysis
    analysis_path = os.path.join(session_dir, "ai_analysis.md")
    try:
        with open(analysis_path, "w") as f:
            f.write(analysis)
    except Exception:
        pass

    # Write manifest
    manifest = {
        "timestamp": timestamp,
        "phase": phase.upper(),
        "git_commit": git_hash,
        "user": os.environ.get("USER", "unknown"),
        "working_dir": CURRENT_LAB_DIR,
        "files_backed_up": backed_up,
    }
    manifest_path = os.path.join(session_dir, "manifest.json")
    try:
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
    except Exception:
        pass

    print(f"\n{'=' * 60}")
    print(f" ARCHIVE CREATED")
    print(f"{'=' * 60}")
    print(f"  Location:   {session_dir}")
    print(f"  Manifest:   {manifest_path}")
    print(f"  Originals:  {originals_dir}/")
    print(f"  AI Report:  {analysis_path}")
    print(f"  Files:      {len(backed_up)} backed up")
    print(f"{'=' * 60}")
    return session_dir


def _save_persistent_log(analysis, target_log_path, config_path,
                         synthesis_issues, hdl_code, timing_report):
    """Legacy text-based log (autota_logs/ in the design dir)."""
    log_dir = os.path.join(CURRENT_LAB_DIR, "autota_logs")
    os.makedirs(log_dir, exist_ok=True)

    original_log_name = os.path.basename(target_log_path)
    log_filename = os.path.join(log_dir, f"autoTA_{original_log_name}")

    try:
        with open(config_path, "r") as f:
            config_raw = f.read()
    except Exception:
        config_raw = "Could not read config file"

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"================ AUTO-TA SESSION | {timestamp} ================\n"
    header += f"SOURCE LOG: {original_log_name}\n"

    combined = [
        header, "\n### 1. GEMINI ANALYSIS ###\n", analysis,
        "\n" + "=" * 60 + "\n", f"### 2. {os.path.basename(config_path)} CONTENT ###\n", config_raw,
        "\n" + "=" * 60 + "\n", "### 3. LOG ISSUES ###\n", synthesis_issues,
        "\n" + "=" * 60 + "\n", "### 4. SOURCE FILES ###\n", hdl_code,
        "\n" + "=" * 60 + "\n", "### 5. TIMING REPORT ###\n", timing_report
    ]

    try:
        with open(log_filename, "w") as f:
            f.write("\n".join(combined))
        print(f" Text log saved: autota_logs/{os.path.basename(log_filename)}")
    except Exception as e:
        print(f" Error saving text log: {e}")


# ==========================================
# MAIN EXECUTION
# ==========================================


def run_shell_command(command):
    try:
        subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, cwd=CURRENT_LAB_DIR)
    except Exception as e:
        print(f" Failed to run command: {e}")


def main():
    print(f"\n Running AutoTA+ for: {os.path.basename(CURRENT_LAB_DIR)} (phase: {PHASE})")
    print("=" * 60)
    run_shell_command(EXTRACT_CMD)

    if not os.path.exists(ISSUES_FILE):
        print(f" Error: {CURRENT_PHASE['issues_file']} was not created.")
        sys.exit(1)

    with open(ISSUES_FILE, "r") as f:
        synthesis_issues = f.read().strip()

    if not synthesis_issues:
        print(" No issues found in this log. Good job!")
        return

    target_files = get_hdl_source_files()
    print(f" Source files: {', '.join(target_files)}")

    hdl_code = ""
    all_source_paths = []  # track all files for archiving
    for filename in target_files:
        content = get_file_content_smart(filename)
        if content:
            hdl_code += f"\n\n//FILE: {filename}\n{content}"
            # Resolve actual path for archiving
            for search in [CURRENT_LAB_DIR,
                           os.path.join(CURRENT_LAB_DIR, "src"),
                           os.path.join(CURRENT_LAB_DIR, "..", "..", "src"),
                           os.path.join(CURRENT_LAB_DIR, "..", "..", "..", "src")]:
                candidate = os.path.join(search, filename)
                if os.path.exists(candidate):
                    all_source_paths.append(candidate)
                    break

    # Gather auxiliary configs and testbench files
    aux_configs = get_auxiliary_config_files()
    testbench_code = get_testbench_files()

    # Track config paths for archiving
    if ACTUAL_CONFIG_PATH and os.path.exists(ACTUAL_CONFIG_PATH):
        all_source_paths.append(ACTUAL_CONFIG_PATH)
    config_dir = os.path.dirname(ACTUAL_CONFIG_PATH) if ACTUAL_CONFIG_PATH else None
    if config_dir:
        for name in aux_configs:
            p = os.path.join(config_dir, name)
            if os.path.exists(p):
                all_source_paths.append(p)

    timing_report_content = get_timing_report_content()

    print("\n Analyzing with Gemini...\n" + "-" * 47)
    analysis = analyze_issues(synthesis_issues, hdl_code, timing_report_content,
                              aux_configs=aux_configs, testbench_code=testbench_code)

    if RICH_AVAILABLE:
        console.print(Markdown(analysis))
    else:
        print(analysis)
    print("\n" + "-" * 47)

    # Archive: back up originals before any patching
    archive_patch_session(analysis, PHASE, all_source_paths)

    log_session(analysis, target_log_path, ACTUAL_CONFIG_PATH,
                synthesis_issues, hdl_code, timing_report_content)


if __name__ == "__main__":
    main()