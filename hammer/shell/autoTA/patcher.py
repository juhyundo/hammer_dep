#!/usr/bin/env python3
"""AutoTA+ Patcher — extracts find-and-replace patches from AI output and applies them.

Used by the Airflow debug tasks to implement the self-healing branch system:
1. Parse structured AI markdown output
2. Extract FILE/FIND/REPLACE_WITH blocks from the ## PATCH section
3. Resolve basenames to actual file paths
4. Apply changes via string replacement
5. Track branch metadata
"""
import os
import re
import json
import time
import shutil

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ==========================================================================
# PARSE AI OUTPUT
# ==========================================================================

def parse_ai_output(analysis: str) -> dict:
    """Extract structured sections from the AI markdown response.

    Returns dict with keys: action, trigger, confidence, patches, changelog, summary
    """
    result = {
        "action": "PROCEED",
        "trigger": {},
        "confidence": "LOW",
        "patches": [],        # list of {file, find, replace} dicts
        "diff": "",           # raw patch text (for archiving)
        "changelog": "",
        "summary": "",
        "fix_level": "",
    }

    # Extract ## SUMMARY
    m = re.search(r'## SUMMARY\s*\n(.*?)(?=\n## |\Z)', analysis, re.DOTALL)
    if m:
        result["summary"] = m.group(1).strip()

    # Extract ## PATCH section and parse FILE/FIND/REPLACE_WITH blocks
    patch_section = re.search(r'## PATCH\s*\n(.*?)(?=\n## |\Z)', analysis, re.DOTALL)
    if patch_section:
        raw_patch = patch_section.group(1).strip()
        result["diff"] = raw_patch  # keep raw for archiving
        result["patches"] = _parse_find_replace_blocks(raw_patch)

    # Fallback: also try ```diff blocks (backward compat)
    if not result["patches"]:
        m = re.search(r'```diff\s*\n(.*?)```', analysis, re.DOTALL)
        if m:
            result["diff"] = m.group(1).strip()

    # Extract ## CHANGE LOG
    m = re.search(r'## CHANGE LOG\s*\n(.*?)(?=\n## |\Z)', analysis, re.DOTALL)
    if m:
        result["changelog"] = m.group(1).strip()

    # Extract ## ACTION fields
    action_block = re.search(r'## ACTION\s*\n(.*?)(?=\n## |\Z)', analysis, re.DOTALL)
    if action_block:
        block = action_block.group(1)

        m = re.search(r'\*\*Result:\*\*\s*(PROCEED|PATCH_AND_RETRY|ABORT)', block)
        if m:
            result["action"] = m.group(1)

        m = re.search(r'\*\*Confidence:\*\*\s*(HIGH|MEDIUM|LOW)', block)
        if m:
            result["confidence"] = m.group(1)

        m = re.search(r'\*\*Fix Level:\*\*\s*(\w+)', block)
        if m:
            result["fix_level"] = m.group(1)

        # Extract Trigger JSON
        m = re.search(r'\*\*Trigger:\*\*\s*`?(\{[^}]+\})`?', block)
        if m:
            try:
                raw = m.group(1).replace('true', 'True').replace('false', 'False')
                result["trigger"] = eval(raw)  # safe: only bool dict
            except Exception:
                pass

    return result


def _parse_find_replace_blocks(text: str) -> list:
    """Parse FILE/FIND/REPLACE_WITH/END_PATCH blocks from AI output.

    Returns list of dicts: [{file, find, replace}]
    """
    patches = []

    # Split on FILE: markers
    file_blocks = re.split(r'\nFILE:\s*', text)
    # Also try with leading FILE: (if it starts the section)
    if text.strip().startswith('FILE:'):
        file_blocks = re.split(r'(?:^|\n)FILE:\s*', text)

    for block in file_blocks:
        block = block.strip()
        if not block:
            continue

        # First line is the filename
        lines = block.split('\n', 1)
        filename = lines[0].strip().strip('`')
        if not filename or len(lines) < 2:
            continue

        remaining = lines[1]

        # Find all FIND/REPLACE_WITH pairs in this block
        # Split on FIND: markers
        find_blocks = re.split(r'\nFIND:\s*\n', remaining)
        if remaining.strip().startswith('FIND:'):
            find_blocks = re.split(r'(?:^|\n)FIND:\s*\n', remaining)

        for fb in find_blocks:
            fb = fb.strip()
            if not fb:
                continue

            # Split on REPLACE_WITH:
            parts = re.split(r'\nREPLACE_WITH:\s*\n', fb, maxsplit=1)
            if len(parts) < 2:
                parts = re.split(r'\nREPLACE_WITH:\s*$', fb, maxsplit=1)
                if len(parts) < 2:
                    continue
                parts.append('')  # empty replacement = delete

            find_text = parts[0].strip()
            replace_raw = parts[1] if len(parts) > 1 else ''

            # Remove END_PATCH marker from the end
            replace_text = re.split(r'\nEND_PATCH\b', replace_raw, maxsplit=1)[0]
            # Don't strip — preserve exact whitespace. Only strip trailing newline.
            replace_text = replace_text.rstrip('\n')

            if find_text:
                patches.append({
                    'file': filename,
                    'find': find_text,
                    'replace': replace_text,
                })

    return patches


# ==========================================================================
# FILE RESOLUTION
# ==========================================================================

def _find_file(basename: str, work_dir: str) -> str:
    """Search for a file by basename in the project tree.

    Searches configs-design/ and src/ directories specifically,
    as these are where Hammer design files live.
    Returns absolute path or empty string.
    """
    abs_work = os.path.abspath(work_dir)

    # Direct match in work_dir
    direct = os.path.join(abs_work, basename)
    if os.path.exists(direct):
        return direct

    # Walk up to find the e2e root (contains configs-design/ and src/)
    parent = abs_work
    for _ in range(6):
        # Check configs-design/ subdirectories
        configs_dir = os.path.join(parent, "configs-design")
        if os.path.isdir(configs_dir):
            for design_name in os.listdir(configs_dir):
                candidate = os.path.join(configs_dir, design_name, basename)
                if os.path.exists(candidate):
                    return os.path.abspath(candidate)

        # Check src/ directory
        src_dir = os.path.join(parent, "src")
        if os.path.isdir(src_dir):
            candidate = os.path.join(src_dir, basename)
            if os.path.exists(candidate):
                return os.path.abspath(candidate)

        # Check direct children
        candidate = os.path.join(parent, basename)
        if os.path.exists(candidate):
            return os.path.abspath(candidate)

        parent = os.path.dirname(parent)

    return ""


# ==========================================================================
# APPLY PATCHES
# ==========================================================================

def apply_patch(diff_text: str, work_dir: str, archive_dir: str,
                patches: list = None) -> dict:
    """Apply find-and-replace patches to source files.

    Args:
        diff_text: Raw AI patch output (archived for reference)
        work_dir: The OBJ_DIR where the flow runs
        archive_dir: Where to save backups and patch records
        patches: Parsed list of {file, find, replace} dicts

    Returns metadata dict with status and details.
    """
    # Save raw AI output
    raw_path = os.path.join(archive_dir, "patch_raw.txt")
    with open(raw_path, "w") as f:
        f.write(diff_text + "\n")

    if not patches:
        return {"applied": False, "reason": "No patches parsed from AI output",
                "diff_path": raw_path}

    applied = []
    failed = []
    backup_dir = os.path.join(archive_dir, "originals")
    os.makedirs(backup_dir, exist_ok=True)

    for patch in patches:
        filename = patch['file']
        find_text = patch['find']
        replace_text = patch['replace']

        # Handle NEW_FILE creation
        if find_text == 'NEW_FILE':
            new_path = os.path.join(work_dir, filename)
            os.makedirs(os.path.dirname(new_path), exist_ok=True)
            with open(new_path, 'w') as f:
                f.write(replace_text + '\n')
            applied.append(f"{filename}: created new file")
            continue

        # Resolve file path
        resolved = _find_file(filename, work_dir)
        if not resolved:
            failed.append(f"{filename}: not found on disk")
            continue

        # Back up original
        backup_path = os.path.join(backup_dir, filename)
        if not os.path.exists(backup_path):
            os.makedirs(os.path.dirname(backup_path), exist_ok=True)
            shutil.copy2(resolved, backup_path)

        # Read file
        with open(resolved, 'r') as f:
            content = f.read()

        # Apply replacement
        if find_text in content:
            new_content = content.replace(find_text, replace_text, 1)
            with open(resolved, 'w') as f:
                f.write(new_content)
            applied.append(f"{filename}: replaced {len(find_text)} chars")
        else:
            # Try fuzzy match (strip trailing whitespace per line)
            fuzzy_result = _fuzzy_replace(content, find_text, replace_text)
            if fuzzy_result is not None:
                with open(resolved, 'w') as f:
                    f.write(fuzzy_result)
                applied.append(f"{filename}: replaced (fuzzy match)")
            else:
                failed.append(f"{filename}: FIND text not found in file")

    # Build result
    if applied and not failed:
        reason = "All patches applied: " + "; ".join(applied)
        return {"applied": True, "reason": reason, "diff_path": raw_path}
    elif applied:
        reason = f"Partial: applied [{', '.join(applied)}], failed [{', '.join(failed)}]"
        return {"applied": True, "reason": reason, "diff_path": raw_path}
    else:
        reason = "No patches applied: " + "; ".join(failed)
        return {"applied": False, "reason": reason, "diff_path": raw_path}


def _fuzzy_replace(content: str, find_text: str, replace_text: str):
    """Try replacing with whitespace-normalized matching.

    Returns new content string or None if no match.
    """
    # Normalize both: collapse whitespace differences
    def normalize(s):
        return '\n'.join(line.rstrip() for line in s.split('\n'))

    norm_content = normalize(content)
    norm_find = normalize(find_text)

    if norm_find in norm_content:
        # Find the position in normalized content
        pos = norm_content.index(norm_find)
        # Map back to original: count characters up to pos in normalized
        # This is safe because we only stripped trailing whitespace
        orig_lines = content.split('\n')
        norm_lines = norm_content.split('\n')

        # Find line range
        char_count = 0
        start_line = 0
        for i, line in enumerate(norm_lines):
            if char_count >= pos:
                start_line = i
                break
            char_count += len(line) + 1  # +1 for \n

        find_line_count = len(norm_find.split('\n'))
        end_line = start_line + find_line_count

        # Replace those lines
        new_lines = (orig_lines[:start_line] +
                     replace_text.split('\n') +
                     orig_lines[end_line:])
        return '\n'.join(new_lines)

    return None


# ==========================================================================
# BRANCH MANAGEMENT
# ==========================================================================

def build_branch_conf(parent_conf: dict, trigger: dict, branch_id: str,
                      parent_run_id: str, confidence: str) -> dict:
    """Build the conf dict for a branched DAG run."""
    conf = {}
    for key in ["syn", "sim_rtl", "par", "sim_syn", "sim_par",
                "power_rtl", "power_syn", "power_par",
                "drc", "lvs", "timing_syn", "timing_par",
                "formal_syn", "formal_par"]:
        conf[key] = trigger.get(key, False)

    conf["build"] = conf.get("syn", False) or conf.get("sim_rtl", False)
    conf["clean"] = False

    conf["branch_id"] = branch_id
    conf["parent_run_id"] = parent_run_id
    conf["confidence"] = confidence
    conf["is_branch"] = True

    return conf


def track_branch(archive_dir: str, branch_id: str, branch_conf: dict,
                 phase_origin: str, patch_result: dict):
    """Record branch info in the central tracking file."""
    tracking_path = os.path.join(SCRIPT_DIR, "branches.json")

    branches = {"branches": []}
    if os.path.exists(tracking_path):
        try:
            with open(tracking_path, "r") as f:
                branches = json.load(f)
        except Exception:
            pass

    entry = {
        "branch_id": branch_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "phase_origin": phase_origin,
        "confidence": branch_conf.get("confidence", "LOW"),
        "trigger_params": {k: v for k, v in branch_conf.items()
                           if k not in ("branch_id", "parent_run_id", "confidence", "is_branch")},
        "parent_run_id": branch_conf.get("parent_run_id", ""),
        "status": "triggered" if patch_result.get("applied") else "failed_to_patch",
        "archive_dir": archive_dir,
    }
    branches["branches"].append(entry)

    with open(tracking_path, "w") as f:
        json.dump(branches, f, indent=2)

    return entry
