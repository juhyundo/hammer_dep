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
SYNTH_ISSUES_FILE = os.path.join(CURRENT_LAB_DIR, "synthesis_issues.log")

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

# 2. Load Lab Design Config (Local settings in labX/design.yml)
def load_lab_config():
    config_path = os.path.join(CURRENT_LAB_DIR, "design.yml")
    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f" Error: Could not find design.yml in {CURRENT_LAB_DIR}")
        sys.exit(1)
    except Exception as e:
        print(f" Error reading design.yml: {e}")
        sys.exit(1)

# Load both configurations
autota_config = load_autota_config()
lab_config = load_lab_config()

# Initialize Client using API Key from config.yml
client = genai.Client(api_key=autota_config["api_key"])
DEFAULT_LOG_DIR = "build/syn-rundir"

# ==========================================
# SMART LOG FILE SELECTION
# ==========================================

def get_target_log_file():
    """
    Determines which log file to read.
    1. Check command line argument.
    2. Check config.yml for custom dir, otherwise use DEFAULT.
    3. Find latest file in that dir.
    """
    # Use config if present, otherwise default
    log_dir_relative = autota_config.get("synth_log_dir", DEFAULT_LOG_DIR)
    log_dir = os.path.join(CURRENT_LAB_DIR, log_dir_relative)
    
    # CASE 1: User specified a filename (e.g. python gemini.py genus.log4)
    if len(sys.argv) > 1:
        requested_file = sys.argv[1]
        full_path = os.path.join(log_dir, requested_file)
        
        if os.path.exists(full_path):
            print(f" Using specified log: {requested_file}")
            return full_path
        else:
            print(f" Error: File '{requested_file}' not found in {log_dir_relative}")
            sys.exit(1)

    # CASE 2: Auto-detect latest file
    if not os.path.exists(log_dir):
        print(f" Error: Log directory not found at: {log_dir_relative}")
        print(" Did you run synthesis? (make synth)")
        sys.exit(1)

    search_pattern = os.path.join(log_dir, "genus.log*") 
    files = glob.glob(search_pattern)

    if not files:
        print(f" Error: No 'genus.log*' files found in {log_dir_relative}")
        sys.exit(1)
        
    latest_file = max(files, key=os.path.getmtime)
    print(f" Using latest log: {os.path.basename(latest_file)}")
    return latest_file

# Get the log path
target_log_path = get_target_log_file()

# Setup extraction command
EXTRACT_SCRIPT_PATH = os.path.join(SCRIPT_DIR, "extract.py")
EXTRACT_CMD = ["python3", EXTRACT_SCRIPT_PATH, target_log_path]

def get_timing_report_content():
    """
    Looks for the timing report in build/syn-rundir/reports/.
    It finds any file ending in .setup_view.rpt
    """
    log_dir_relative = autota_config.get("synth_log_dir", DEFAULT_LOG_DIR)
    reports_dir = os.path.join(CURRENT_LAB_DIR, log_dir_relative, "reports")
    
    if not os.path.exists(reports_dir):
        return "No reports directory found (Synthesis might have failed early)."

    # Look for the specific pattern provided
    pattern = os.path.join(reports_dir, "*.setup_view.rpt")
    files = glob.glob(pattern)

    if not files:
        return "No '.setup_view.rpt' timing report found."
    
    # Use the most recent report if multiple exist
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
    """
    Finds the list of Verilog files from the LAB config (design.yml).
    """
    # Check standard 'synthesis.inputs' in design.yml
    synth_inputs = lab_config.get("synthesis.inputs", {})
    if "input_files" in synth_inputs:
        return synth_inputs["input_files"]

    print(" Error: Could not find 'input_files' in synthesis.inputs inside design.yml.")
    sys.exit(1)

def get_file_content_smart(filename):
    path_a = os.path.join(CURRENT_LAB_DIR, filename)       # e.g. "src/divider.v"
    path_b = os.path.join(CURRENT_LAB_DIR, "src", filename) # e.g. "divider.v"

    if os.path.exists(path_a):
        with open(path_a, "r") as f: return f.read()
    elif os.path.exists(path_b):
        with open(path_b, "r") as f: return f.read()
    else:
        print(f" Warning: Could not find source file: {filename}")
        return None

def analyze_issues(synthesis_issues, hdl_code, timing_report_content):
    # Load AI settings from config.yml
    settings = autota_config.get("ai_settings", {})
    model_name = settings.get("model", "gemini-3-flash-preview")
    prompt_text = settings.get("prompt", "Analyze these issues.")

    # NEW: Read the raw design.yml file to send to Gemini
    design_yml_path = os.path.join(CURRENT_LAB_DIR, "design.yml")
    design_yml_raw = ""
    try:
        with open(design_yml_path, "r") as f:
            design_yml_raw = f.read()
    except Exception as e:
        design_yml_raw = f"Error reading design.yml: {e}"

    full_prompt = (
        f"{prompt_text}\n"
        "---------------------------------------------------\n"
        "DESIGN.YML CONTENT:\n"
        f"{design_yml_raw}\n\n"
        "LOG ISSUES:\n"
        f"{synthesis_issues}\n\n"
        "VERILOG SOURCE:\n"
        f"{hdl_code}\n\n"
        "TIMING REPORT:\n"
        f"{timing_report_content}"
    )
    
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=full_prompt
        )
        return response.text
    except Exception as e:
        return f"API Error: {e}"

def save_persistent_log(analysis, target_log_path, design_yml_path, synthesis_issues, hdl_code, timing_report):
    """
    Saves a log containing Gemini output and context.
    Naming convention: autoTA_<original_log_name>
    """
    log_dir = os.path.join(CURRENT_LAB_DIR, "autota_logs")
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # Extract the base name (e.g., genus.log9) and prefix it
    original_log_name = os.path.basename(target_log_path)
    log_filename = os.path.join(log_dir, f"autoTA_{original_log_name}")

    # Read raw design.yml for the log
    try:
        with open(design_yml_path, "r") as f:
            design_yml_raw = f.read()
    except:
        design_yml_raw = "Could not read design.yml"

    # Construct the combined log content
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"================ AUTO-TA SESSION | {timestamp} ================\n"
    header += f"SOURCE LOG: {original_log_name}\n"
    
    combined_content = [
        header,
        "\n### 1. GEMINI ANALYSIS ###\n",
        analysis,
        "\n" + "="*60 + "\n",
        "### 2. DESIGN.YML CONTENT ###\n",
        design_yml_raw,
        "\n" + "="*60 + "\n",
        "### 3. SYNTHESIS LOG (ISSUES) ###\n",
        synthesis_issues,
        "\n" + "="*60 + "\n",
        "### 4. VERILOG SOURCE FILES ###\n",
        hdl_code,
        "\n" + "="*60 + "\n",
        "### 5. TIMING REPORT ###\n",
        timing_report
    ]

    # Write to file
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
    print(f" Running AutoTA for: {os.path.basename(CURRENT_LAB_DIR)}")

    # 1. Run Extraction
    run_shell_command(EXTRACT_CMD)

    # 2. Check Issues
    if not os.path.exists(SYNTH_ISSUES_FILE):
        print(" Error: synthesis_issues.log was not created.")
        sys.exit(1)

    with open(SYNTH_ISSUES_FILE, "r") as f:
        synthesis_issues = f.read().strip()

    if not synthesis_issues:
        print(" No synthesis issues found in this log. Good job!")
        return

    # 3. Read Source Files (From design.yml)
    target_files = get_hdl_source_files()
    files_str = ", ".join(target_files)
    print(f" Found issues. Analyzing {files_str} source files...")

    hdl_code = ""
    for filename in target_files:
        content = get_file_content_smart(filename)
        if content:
            hdl_code += f"\n\n//FILE: {filename}\n{content}"

    # 4. Get Timing Report Content
    timing_report_content = get_timing_report_content()
    
    # 5. Gemini Analysis (Using settings from config.yml)
    print("\n Gemini Analysis:\n")
    print("-----------------------------------------------")
    analysis = analyze_issues(synthesis_issues, hdl_code, timing_report_content)

    if RICH_AVAILABLE:
        console.print(Markdown(analysis))
    else:
        print(analysis)
    print("\n-----------------------------------------------")

    # 6. SAVE LOG (Updated with target_log_path)
    design_yml_path = os.path.join(CURRENT_LAB_DIR, "design.yml")
    save_persistent_log(
        analysis, 
        target_log_path, # This variable is defined globally in your script via get_target_log_file()
        design_yml_path, 
        synthesis_issues, 
        hdl_code, 
        timing_report_content
    )

def run_shell_command(command):
    try:
        subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=CURRENT_LAB_DIR)
    except Exception as e:
        print(f" Failed to run command: {e}")

if __name__ == "__main__":
    main()