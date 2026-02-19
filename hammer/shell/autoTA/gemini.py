import sys
import os
import subprocess
from datetime import datetime
import yaml
import glob

# ==========================================
# AUTO-SETUP BLOCK (Start)
# ==========================================
# If imports fail, this block restarts the script inside the correct Conda env.

# Get the directory where this script is located (e.g., .../autoTA)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Check if conda is inside SCRIPT_DIR (e.g. autoTA/conda)
if os.path.exists(os.path.join(SCRIPT_DIR, 'conda')):
    WORKSPACE_PATH = SCRIPT_DIR
else:
    # Go up one level to find the workspace root (e.g., .../asic-labs-fa25-isabelleh123)
    WORKSPACE_PATH = os.path.dirname(SCRIPT_DIR)

try:
    # Attempt to import critical libraries
    from google import genai
except ImportError:
    # Check for recursion
    if os.environ.get('GEMINI_AUTO_SETUP') == '1':
        print('Error: Auto-setup failed to fix imports. Aborting infinite loop.')
        sys.exit(1)

    # If imports fail, we assume the environment is not set up.
    print('----------------------------------------------------------')
    print(' Conda Environment not detected -- setting up Conda automatically.')
    print('----------------------------------------------------------')

    # Get the absolute path of this script and any arguments passed to it
    script_abs_path = os.path.abspath(__file__)
    script_args = ' '.join(sys.argv[1:])

    # Construct the Bash command to set up the env and re-run this script
    cmd = (
        f'export MY_WS={WORKSPACE_PATH}; '
        f'export GEMINI_AUTO_SETUP=1; '
        f'eval "$(${{MY_WS}}/conda/bin/conda shell.bash hook)"; '
        'conda activate; '
        f'python3 "{script_abs_path}" {script_args}'
    )

    # Replace the current process with the new one
    try:
        os.execv('/bin/bash', ['bash', '-c', cmd])
    except Exception as e:
        print(f'Failed to auto-setup environment: {e}')
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
SYNTH_ISSUES_FILE = os.path.join(CURRENT_LAB_DIR, 'synthesis_issues.log')

# Global variable to store the found design config path
DESIGN_CONFIG_PATH = None

# 1. Load AutoTA Config (Global settings in autoTA/config.yml)
def load_autota_config():
    config_path = os.path.join(SCRIPT_DIR, 'config.yml')
    try:
        with open(config_path, 'r') as f:
            data = yaml.safe_load(f)
        if 'autota' not in data:
            print(f' Error: 'autota' section missing in {config_path}')
            sys.exit(1)
        return data['autota']
    except FileNotFoundError:
        print(f' Error: Could not find config.yml at {config_path}')
        sys.exit(1)
    except Exception as e:
        print(f' Error reading config.yml: {e}')
        sys.exit(1)

# 2. Load Lab Design Config (Local settings in labX/design.yml or syn.yml)
def load_lab_config():
    global DESIGN_CONFIG_PATH
    design_name = os.path.basename(CURRENT_LAB_DIR)
    
    # Priority list for configuration files
    candidates = [
        os.path.join(CURRENT_LAB_DIR, 'syn.yml'),
        os.path.join(CURRENT_LAB_DIR, 'design.yml'),
        os.path.abspath(os.path.join(CURRENT_LAB_DIR, '../../configs-design', design_name, 'syn.yml'))
    ]
    
    for config_path in candidates:
        if os.path.exists(config_path):
            print(f' Loading design config from: {config_path}')
            DESIGN_CONFIG_PATH = config_path
            try:
                with open(config_path, 'r') as f:
                    return yaml.safe_load(f)
            except Exception as e:
                print(f' Error reading {config_path}: {e}')
                sys.exit(1)

    print(f' Error: Could not find syn.yml or design.yml in {CURRENT_LAB_DIR} or standard locations.')
    sys.exit(1)

# Load both configurations
autota_config = load_autota_config()
lab_config = load_lab_config()

# Initialize Client using API Key from config.yml
client = genai.Client(api_key=autota_config['api_key'])
current_script_dir = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG_DIR = 'syn-rundir' # Changed to relative syn-rundir

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
    log_dir_relative = autota_config.get('synth_log_dir', DEFAULT_LOG_DIR)
    log_dir = os.path.join(CURRENT_LAB_DIR, log_dir_relative)
    
    # CASE 1: User specified a filename (e.g. python gemini.py genus.log4)
    if len(sys.argv) > 1:
        requested_file = sys.argv[1]
        full_path = os.path.join(log_dir, requested_file)
        
        if os.path.exists(full_path):
            print(f' Using specified log: {requested_file}')
            return full_path
        else:
            print(f' Error: File '{requested_file}' not found in {log_dir_relative}')
            sys.exit(1)

    # CASE 2: Auto-detect latest file
    if not os.path.exists(log_dir):
        print(f' Error: Log directory not found at: {log_dir_relative}')
        print(' Did you run synthesis? (make synth)')
        sys.exit(1)

    search_pattern = os.path.join(log_dir, 'genus.log*') 
    files = glob.glob(search_pattern)

    if not files:
        print(f' Error: No 'genus.log*' files found in {log_dir_relative}')
        sys.exit(1)
        
    latest_file = max(files, key=os.path.getmtime)
    print(f' Using latest log: {os.path.basename(latest_file)}')
    return latest_file

# Get the log path
target_log_path = get_target_log_file()

# Setup extraction command
EXTRACT_SCRIPT_PATH = os.path.join(SCRIPT_DIR, 'extract.py')
EXTRACT_CMD = ['python3', EXTRACT_SCRIPT_PATH, target_log_path]

def get_timing_report_content():
    """
    Looks for the timing report in build/syn-rundir/reports/.
    It finds any file ending in .setup_view.rpt
    """
    log_dir_relative = autota_config.get('synth_log_dir', DEFAULT_LOG_DIR)
    reports_dir = os.path.join(CURRENT_LAB_DIR, log_dir_relative, 'reports')
    
    if not os.path.exists(reports_dir):
        return 'No reports directory found (Synthesis might have failed early).'

    # Look for the specific pattern provided
    pattern = os.path.join(reports_dir, '*.setup_view.rpt')
    files = glob.glob(pattern)

    if not files:
        return 'No '.setup_view.rpt' timing report found.'
    
    # Use the most recent report if multiple exist
    latest_report = max(files, key=os.path.getmtime)
    print(f' Including Timing Report: {os.path.basename(latest_report)}')
    
    try:
        with open(latest_report, 'r') as f:
            return f.read()
    except Exception as e:
        return f'Error reading timing report: {e}'

# ==========================================
# FILE RETRIEVAL & AI LOGIC
# ==========================================

def get_hdl_source_files():
    """
    Finds the list of Verilog files from the LAB config (design.yml).
    """
    # Check standard 'synthesis.inputs' in design.yml
    synth_inputs = lab_config.get('synthesis.inputs', {})
    if 'input_files' in synth_inputs:
        return synth_inputs['input_files']

    print(' Error: Could not find 'input_files' in synthesis.inputs inside design.yml.')
    sys.exit(1)

def get_file_content_smart(filename):
    """
    Tries to find the file content by searching in multiple locations.
    """
    paths = [
        filename, # As provided (could be absolute)
        os.path.join(CURRENT_LAB_DIR, filename),
        os.path.join(CURRENT_LAB_DIR, 'src', os.path.basename(filename)),
        os.path.abspath(os.path.join(CURRENT_LAB_DIR, '../../src', os.path.basename(filename))) # Standard e2e src loc
    ]

    for path in paths:
        if os.path.exists(path):
            try:
                with open(path, 'r') as f: return f.read()
            except:
                pass
                
    print(f' Warning: Could not find source file: {filename}')
    return None

def analyze_issues(synthesis_issues, hdl_code, timing_report_content):
    # Load AI settings from config.yml
    settings = autota_config.get('ai_settings', {})
    model_name = settings.get('model', 'gemini-3-flash-preview')
    prompt_text = settings.get('prompt', 'Analyze these issues.')

    # Read the raw design config file (DESIGN_CONFIG_PATH is set in load_lab_config)
    design_yml_raw = ''
    if DESIGN_CONFIG_PATH and os.path.exists(DESIGN_CONFIG_PATH):
        try:
            with open(DESIGN_CONFIG_PATH, 'r') as f:
                design_yml_raw = f.read()
        except Exception as e:
            design_yml_raw = f'Error reading design config: {e}'
    else:
        design_yml_raw = 'Design config file not found.'

    full_prompt = (
        f'{prompt_text}
'
        '---------------------------------------------------
'
        'DESIGN CONFIG CONTENT:
'
        f'{design_yml_raw}

'
        'LOG ISSUES:
'
        f'{synthesis_issues}

'
        'VERILOG SOURCE:
'
        f'{hdl_code}

'
        'TIMING REPORT:
'
        f'{timing_report_content}'
    )
    
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=full_prompt
        )
        return response.text
    except Exception as e:
        return f'API Error: {e}'

def save_persistent_log(analysis, target_log_path, design_yml_path, synthesis_issues, hdl_code, timing_report):
    """
    Saves a log containing Gemini output and context.
    Naming convention: autoTA_<original_log_name>
    """
    log_dir = os.path.join(CURRENT_LAB_DIR, 'autota_logs')
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # Extract the base name (e.g., genus.log9) and prefix it
    original_log_name = os.path.basename(target_log_path)
    log_filename = os.path.join(log_dir, f'autoTA_{original_log_name}')

    # Read raw design.yml for the log
    try:
        if design_yml_path and os.path.exists(design_yml_path):
             with open(design_yml_path, 'r') as f:
                design_yml_raw = f.read()
        else:
             design_yml_raw = 'Design config not found'
    except:
        design_yml_raw = 'Could not read design config'

    # Construct the combined log content
    timestamp = datetime.now().strftime('