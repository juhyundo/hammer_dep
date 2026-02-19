import re
import sys
from collections import defaultdict

# Check if file path is provided as command-line argument
if len(sys.argv) < 2:
  print("Usage: python extract.py <log_file_path> [output_file_path]")
  print("Example: python extract.py synth.log")
  print("Example: python extract.py synth.log custom_output.log")
  sys.exit(1)

# File paths
file_path = sys.argv[1]
output_file_path = sys.argv[2] if len(sys.argv) > 2 else 'synthesis_issues.log'

# Define issue categories with associated keywords and severity levels
categories_info = {
 "Undeclared Signals": (["implicitly declared"], "Major"),
 "Missing Drivers": (["has no driver"], "Critical"),
 "Latch Issues": (["Latch inferred"], "Major"),
 "Conflicting Drivers": (["multiple conflicting drivers", "Driver - driver conflict"], "Critical"),
 "Combinational Loop": (["combinational loop", "logic loop"], "Critical"),
 "Optimized Out": (["removing D path"], "Minor"),
 "Unsynthesizable": (["not synthesizable", "unsynthesizable"], "Critical"),
 "Errors": (["Error", "ERROR", "error"], "Critical"),
 "Warnings": (["Warning", "WARNING", "warning"], "Major"),
 "Resource Utilization": (["Creating decoders for process"], "Minor"),
 "Hierarchy/Blackbox": (["black box", "module not found", "unresolved"], "Critical"), #New
 "Sensitivity List": (["incomplete sensitivity", "missing from sensitivity"], "Major"), #New
}

# Data structure for storing categorized issues
found_issues = defaultdict(lambda: {"logs": set(), "severity": None})

def process_log_entry(entry, category, issues_reference):
  """Adds an issue entry to the category while ensuring uniqueness."""
  if entry not in issues_reference[category]["logs"]:
    issues_reference[category]["logs"].add(entry)
    if issues_reference[category]["severity"] is None:
      issues_reference[category]["severity"] = categories_info[category][1]

def analyze_log(log_lines):
  """Processes log lines to extract categorized issues."""
  idx = 0
  total_lines = len(log_lines)

  while idx < total_lines:
    line = log_lines[idx].strip()

    if not line:
      idx += 1
      continue # Skip empty lines

    category_matched = None # Stores the matched category

    for category, (keywords, severity) in categories_info.items():
      keyword_match = any(re.search(rf'\b{re.escape(k)}\b', line, re.IGNORECASE) for k in keywords)

      if keyword_match:
        # Capture multi-line issue descriptions if applicable
        issue_block = [line]
        idx += 1
        while idx < total_lines and log_lines[idx].startswith((' ', '\t')):
          issue_block.append(log_lines[idx].rstrip())
          idx += 1

        full_issue = "\n".join(issue_block)
        
        # --- NEW CODE: Replace PDK names ---
        # Matches 'sky' or 'tsmc' followed by zero or more digits (case insensitive)
        pdk_pattern = r'(sky\d*|ts\d*)'
        full_issue = re.sub(pdk_pattern, 'PDK', full_issue, flags=re.IGNORECASE)
        # -----------------------------------

        # Check if this exact issue block already exists in this category
        if full_issue not in found_issues[category]["logs"]:
          process_log_entry(full_issue, category, found_issues)
        
        category_matched = category
        break # Ensure each log entry is categorized only once

    if category_matched is None:
      idx += 1 # Move to the next line if no match was found

# Execute log analysis
try:
  with open(file_path, 'r') as f:
    analyze_log(f.readlines())
except FileNotFoundError:
  print(f"Error: File '{file_path}' not found.")
  sys.exit(1)
except Exception as e:
  print(f"Error reading file: {e}")
  sys.exit(1)

# Write categorized issues to an output file
try:
  with open(output_file_path, 'w') as output_file:
    for category, data in sorted(found_issues.items(), key=lambda x: x[1]["severity"] or "Z"):
      logs = data["logs"]
      if logs:
        output_file.write(f"{category} (Severity: {data['severity']}):\n")
        for log_entry in sorted(logs, key=lambda x: len(x)): # Sorted for better readability
          output_file.write("  " + log_entry.replace("\n", "\n  ") + "\n")
        output_file.write("\n")
  print(f"Synthesis issues have been saved to {output_file_path}")
except Exception as e:
  print(f"Error writing output file: {e}")
  sys.exit(1)
