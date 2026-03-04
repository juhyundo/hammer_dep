import re
import sys
from collections import defaultdict

# Parse args with minimal disruption to existing positional args
phase = "syn"
positional = []
i = 1
while i < len(sys.argv):
  if sys.argv[i] == "--phase":
    if i + 1 >= len(sys.argv):
      print("Usage: python extract.py <log_file_path> [output_file_path] [--phase syn|par]")
      sys.exit(1)
    phase = sys.argv[i + 1].lower()
    i += 2
  else:
    positional.append(sys.argv[i])
    i += 1

# Check if file path is provided as command-line argument
if len(positional) < 1:
  print("Usage: python extract.py <log_file_path> [output_file_path] [--phase syn|par]")
  print("Example: python extract.py synth.log")
  print("Example: python extract.py synth.log custom_output.log")
  print("Example: python extract.py par.log --phase par")
  sys.exit(1)

if phase not in ["syn", "par"]:
  print("Error: --phase must be 'syn' or 'par'")
  sys.exit(1)

# File paths
file_path = positional[0]
default_out = "synthesis_issues.log" if phase == "syn" else "par_issues.log"
output_file_path = positional[1] if len(positional) > 1 else default_out

# Define issue categories with associated keywords and severity levels
syn_categories_info = {
  "Undeclared Signals": (["implicitly declared"], "Major"),
  "Missing Drivers": (["has no driver"], "Critical"),
  "Latch Issues": (["Latch inferred"], "Critical"),
  "Conflicting Drivers": (["multiple conflicting drivers", "Driver - driver conflict"], "Critical"),
  "Combinational Loop": (["combinational loop", "logic loop"], "Critical"),
  "Optimized Out": (["removing D path"], "Minor"),
  "Unsynthesizable": (["not synthesizable", "unsynthesizable"], "Major"),
  "Hierarchy/Blackbox": (["black box", "module not found", "unresolved"], "Major"),
  "Sensitivity List": (["incomplete sensitivity", "missing from sensitivity"], "Major"),
  "Resource Utilization": (["Creating decoders for process"], "Minor"),
  "Errors": (["Error", "ERROR", "error"], "Critical"),
  "Warnings": (["Warning", "WARNING", "warning"], "Major"),
}

par_categories_info = {
  "PAR Timing Setup Violations": (["Setup Check", "setup violation", "negative slack", "timing constraint violated"], "Critical"),
  "PAR Timing Hold Violations": (["Hold Check", "hold violation", "negative hold slack"], "Critical"),
  "DRC Violations": (["DRC violation", "short circuit", "open circuit"], "Major"),
  "Library / Tech File Errors": (["cannot open lef", "missing lef", "layer not found", "technology file not found"], "Major"),
  "Routing / Congestion": (["congestion", "overflow", "global route", "detailed route", "route failed", "cannot route", "unroutable", "detour"], "Critical"),
  "Errors": (["Error", "ERROR", "error"], "Critical"),
  "Warnings": (["Warning", "WARNING", "warning"], "Major"),
}

categories_info = syn_categories_info if phase == "syn" else par_categories_info

# Data structure for storing categorized issues
found_issues = defaultdict(lambda: {"logs": set(), "severity": None})

def mask_pdk_info(text):
  """
  Mask PDK-specific information to prevent NDA leaks.
  This is more aggressive to catch various PDK naming patterns.
  """
  pdk_patterns = [
    (r'\bsky130\w*', 'PDK'),
    (r'\bsky\d+\w*', 'PDK'),
    (r'\bts\d+\w*', 'PDK'),
    (r'\btsmc\w*', 'PDK'),
    (r'\basap\d+\w*', 'PDK'),
    (r'\bsaed\d+\w*', 'PDK'),
    (r'/\w+/PDK/\w+', '/PDK/path'),
    (r'sky130_\w+_\w+', 'PDK_lib'),
  ]

  result = text
  for pattern, replacement in pdk_patterns:
    result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

  return result

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
      continue  # Skip empty lines

    category_matched = None  # Stores the matched category

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

        # Mask PDK information
        full_issue = mask_pdk_info(full_issue)

        # Check if this exact issue block already exists in this category
        if full_issue not in found_issues[category]["logs"]:
          process_log_entry(full_issue, category, found_issues)

        category_matched = category
        break  # Ensure each log entry is categorized only once

    if category_matched is None:
      idx += 1  # Move to the next line if no match was found

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
        for log_entry in sorted(logs, key=lambda x: len(x)):  # Sorted for better readability
          output_file.write("  " + log_entry.replace("\n", "\n  ") + "\n")
        output_file.write("\n")
  print(f"{phase.upper()} issues have been saved to {output_file_path}")
except Exception as e:
  print(f"Error writing output file: {e}")
  sys.exit(1)