import sys
import os
import subprocess
from datetime import datetime
import sys
import os
import subprocess
from datetime import datetime

# ==========================================
# AUTO-SETUP BLOCK (Start)
# ==========================================
# If imports fail, this block restarts the script inside the correct Conda env.

# Get the directory where this script is located (e.g., .../autoTA)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    # Attempt to import critical libraries
    from google import genai
    import yaml
    import glob
except ImportError:
    # 2. RECURSION GUARD: Prevent infinite loops if the environment is broken
    if os.environ.get("GEMINI_AUTO_SETUP") == "1":
        print(" Error: Auto-setup loop detected. The Conda environment failed to load dependencies.")
        sys.exit(1)

    # If imports fail, we assume the environment is not set up.
    print("----------------------------------------------------------")
    print(" Conda Environment not detected -- setting up Conda automatically.")
    print("----------------------------------------------------------")

    # Get the absolute path of this script and any arguments passed to it
    script_abs_path = os.path.abspath(__file__)
    script_args = " ".join(sys.argv[1:])

    # 1. FIXED PATH: Point directly to the conda folder inside autoTA
    conda_dir = os.path.join(SCRIPT_DIR, "conda")

    # 3. CLEANED UP CODE: Construct the Bash command with the recursion flag
    cmd = (
        f"export GEMINI_AUTO_SETUP=1; "
        f"eval \"$({conda_dir}/bin/conda shell.bash hook)\"; "
        "conda activate; "
        f"python3 \"{script_abs_path}\" {script_args}"
    )

    # Replace the current process with the new one
    try:
        os.execv("/bin/bash", ["bash", "-c", cmd])
    except Exception as e:
        print(f"Failed to auto-setup environment: {e}")
        sys.exit(1)

# ==========================================
# AUTO-SETUP BLOCK (End)
# ==========================================

# Standard imports
try:
    from rich.console import Console
    from rich.markdown import Markdown
    RICH_AVAILABLE = True
    console = Console()
except ImportError:
    RICH_AVAILABLE = False

# ==========================================
# CONFIGURATION LOADING
# ==========================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CURRENT_LAB_DIR = os.getcwd()

# ==========================================
# PHASE ARGUMENT
# ==========================================
import argparse
_parser = argparse.ArgumentParser(description="AutoTA AI log analyzer")
_parser.add_argument("--phase", default="syn", choices=["syn", "sim_rtl", "par"],
                     help="VLSI phase to analyze (default: syn)")
_parser.add_argument("logfile", nargs="?", default=None, help="Specific log file to analyze")
_args, _remaining = _parser.parse_known_args()
PHASE = _args.phase

# Phase-dependent settings
PHASE_CONFIG = {
    "syn":     {"log_dir": "syn-rundir",     "log_glob": "genus.log*",   "config_name": "syn.yml",     "issues_file": "synthesis_issues.log", "prompt_key": "prompt"},
    "sim_rtl": {"log_dir": "sim-rundir",     "log_glob": "*.log",        "config_name": "sim-rtl.yml", "issues_file": "sim_issues.log",       "prompt_key": "sim_rtl_prompt"},
    "par":     {"log_dir": "par-rundir",     "log_glob": "innovus.log*", "config_name": "par.yml",     "issues_file": "par_issues.log",       "prompt_key": "par_prompt"},
}
CURRENT_PHASE = PHASE_CONFIG[PHASE]
ISSUES_FILE = os.path.join(CURRENT_LAB_DIR, CURRENT_PHASE["issues_file"])

# 1. Load AutoTA Config (Global settings in autoTA/config.yml)
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

# 2. Load Lab Design Config (phase-aware: looks for syn.yml, sim-rtl.yml, or par.yml)
def load_lab_config():
    global ACTUAL_CONFIG_PATH
    config_name = CURRENT_PHASE["config_name"]
    abs_lab_dir = os.path.abspath(CURRENT_LAB_DIR)
    dir_parts = abs_lab_dir.split(os.sep)
    
    # Base fallback searches
    search_options = [
        config_name, 
        f"../{config_name}", 
        f"../../{config_name}"
    ]

    # Dynamically inject Hammer E2E paths
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
                
    print(f" Error: Could not find {config_name} relative to {CURRENT_LAB_DIR}")
    print(" Proceeding without a design config file.")
    return {}

# Load both configurations
autota_config = load_autota_config()
lab_config = load_lab_config()

try:
    client = genai.Client(api_key=autota_config["api_key"])
except Exception as e:
    print(f" Error initializing Gemini Client: {e}")
    print(" Proceeding but AI analysis will fail.")
    client = None

DEFAULT_LOG_DIR = CURRENT_PHASE["log_dir"]
ACTUAL_CONFIG_PATH = "" # Store this globally so the log saver knows which one we used

# Removed duplicate load_lab_config

# ==========================================
# SMART LOG FILE SELECTION
# ==========================================

def get_target_log_file():
    log_dir_relative = autota_config.get("synth_log_dir", DEFAULT_LOG_DIR)
    log_dir = os.path.join(CURRENT_LAB_DIR, log_dir_relative)
    
    # If a specific logfile was passed via --phase arg parsing
    if _args.logfile:
        full_path = os.path.join(log_dir, _args.logfile)
        if os.path.exists(full_path):
            return full_path
        else:
            print(f" Error: File '{_args.logfile}' not found in {log_dir_relative}")
            sys.exit(1)

    if not os.path.exists(log_dir):
        print(f" Error: Log directory not found at: {log_dir_relative}")
        sys.exit(1)

    # Grab the latest log matching phase-specific glob
    log_glob = CURRENT_PHASE["log_glob"]
    search_pattern = os.path.join(log_dir, log_glob) 
    files = glob.glob(search_pattern)

    if not files:
        print(f" Error: No '{log_glob}' files found in {log_dir_relative}")
        sys.exit(1)
        
    latest_file = max(files, key=os.path.getmtime)
    print(f" Using latest log: {os.path.basename(latest_file)}")
    return latest_file

target_log_path = get_target_log_file()
EXTRACT_SCRIPT_PATH = os.path.join(SCRIPT_DIR, "extract.py")
EXTRACT_CMD = ["python3", EXTRACT_SCRIPT_PATH, "--phase", PHASE, target_log_path]

def get_timing_report_content():
    log_dir_relative = autota_config.get("synth_log_dir", DEFAULT_LOG_DIR)
    reports_dir = os.path.join(CURRENT_LAB_DIR, log_dir_relative, "reports")
    
    if not os.path.exists(reports_dir):
        return "No reports directory found (Synthesis might have failed early)."

    pattern = os.path.join(reports_dir, "*.setup_view.rpt")
    files = glob.glob(pattern)

    if not files:
        return "No '.setup_view.rpt' timing report found."
    
    latest_report = max(files, key=os.path.getmtime)
    print(f" Including Timing Report: {os.path.basename(latest_report)}")
    
    try:
        with open(latest_report, "r") as f:
            return f.read()
    except Exception as e:
        return f"Error reading timing report: {e}"

# ==========================================
# FILE RETRIEVAL & AI LOGIC
# ==========================================

def get_hdl_source_files():
    synth_inputs = lab_config.get("synthesis.inputs", {}) if lab_config else {}
    if "input_files" in synth_inputs:
        return synth_inputs["input_files"]
    print(" Warning: Could not find 'input_files' in synthesis.inputs. Proceeding without HDL source files.")
    return []

def get_file_content_smart(filename):
    # Flexible search paths for E2E Hammer builds
    search_paths = [
        os.path.join(CURRENT_LAB_DIR, filename),
        os.path.join(CURRENT_LAB_DIR, "src", filename),
        os.path.join(CURRENT_LAB_DIR, "..", "..", "src", filename),
        os.path.join(CURRENT_LAB_DIR, "..", "..", "..", "src", filename)
    ]

    for path in search_paths:
        if os.path.exists(path):
            with open(path, "r") as f: return f.read()
            
    print(f" Warning: Could not find source file: {filename}")
    return None

def analyze_issues(synthesis_issues, hdl_code, timing_report_content):
    settings = autota_config.get("ai_settings", {})
    model_name = settings.get("model", "gemini-3-flash-preview")
    prompt_key = CURRENT_PHASE["prompt_key"]
    prompt_text = settings.get(prompt_key, settings.get("prompt", "Analyze these issues."))

    # Safely read the config file we actually found earlier
    try:
        with open(ACTUAL_CONFIG_PATH, "r") as f:
            config_raw = f.read()
    except Exception as e:
        config_raw = f"Error reading config: {e}"

    full_prompt = (
        f"{prompt_text}\n"
        "---------------------------------------------------\n"
        f"CONFIG ({os.path.basename(ACTUAL_CONFIG_PATH)}) CONTENT:\n"
        f"{config_raw}\n\n"
        "LOG ISSUES:\n"
        f"{synthesis_issues}\n\n"
        "VERILOG SOURCE:\n"
        f"{hdl_code}\n\n"
        "TIMING REPORT:\n"
        f"{timing_report_content}"
    )
    
    try:
        response = client.models.generate_content(model=model_name, contents=full_prompt)
        return response.text
    except Exception as e:
        return f"API Error: {e}"

def save_persistent_log(analysis, target_log_path, config_path, synthesis_issues, hdl_code, timing_report):
    log_dir = os.path.join(CURRENT_LAB_DIR, "autota_logs")
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    original_log_name = os.path.basename(target_log_path)
    log_filename = os.path.join(log_dir, f"autoTA_{original_log_name}")

    try:
        with open(config_path, "r") as f:
            config_raw = f.read()
    except:
        config_raw = "Could not read config file"

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"================ AUTO-TA SESSION | {timestamp} ================\n"
    header += f"SOURCE LOG: {original_log_name}\n"
    
    combined_content = [
        header, "\n### 1. GEMINI ANALYSIS ###\n", analysis,
        "\n" + "="*60 + "\n", f"### 2. {os.path.basename(config_path)} CONTENT ###\n", config_raw,
        "\n" + "="*60 + "\n", "### 3. SYNTHESIS LOG (ISSUES) ###\n", synthesis_issues,
        "\n" + "="*60 + "\n", "### 4. VERILOG SOURCE FILES ###\n", hdl_code,
        "\n" + "="*60 + "\n", "### 5. TIMING REPORT ###\n", timing_report
    ]

    try:
        with open(log_filename, "w") as f:
            f.write("\n".join(combined_content))
        print(f" Session log saved to: autota_logs/{os.path.basename(log_filename)}")
    except Exception as e:
        print(f" Error saving persistent log: {e}")

# ==========================================
# MAIN EXECUTION
# ==========================================

def main():
    print(f" Running AutoTA for: {os.path.basename(CURRENT_LAB_DIR)} (phase: {PHASE})")
    run_shell_command(EXTRACT_CMD)

    if not os.path.exists(ISSUES_FILE):
        print(f" Error: {CURRENT_PHASE['issues_file']} was not created.")
        sys.exit(1)

    with open(ISSUES_FILE, "r") as f:
        synthesis_issues = f.read().strip()

    if not synthesis_issues:
        print(" No synthesis issues found in this log. Good job!")
        return

    target_files = get_hdl_source_files()
    files_str = ", ".join(target_files)
    print(f" Found issues. Analyzing {files_str} source files...")

    hdl_code = ""
    for filename in target_files:
        content = get_file_content_smart(filename)
        if content:
            hdl_code += f"\n\n//FILE: {filename}\n{content}"

    timing_report_content = get_timing_report_content()
    
    print("\n Gemini Analysis:\n" + "-"*47)
    analysis = analyze_issues(synthesis_issues, hdl_code, timing_report_content)

    if RICH_AVAILABLE:
        console.print(Markdown(analysis))
    else:
        print(analysis)
    print("\n" + "-"*47)

    save_persistent_log(analysis, target_log_path, ACTUAL_CONFIG_PATH, synthesis_issues, hdl_code, timing_report_content)

def run_shell_command(command):
    try:
        subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=CURRENT_LAB_DIR)
    except Exception as e:
        print(f" Failed to run command: {e}")

if __name__ == "__main__":
    main()