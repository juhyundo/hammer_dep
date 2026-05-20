# Sledgehammer AutoTA Debugging History (2026-03-05)

## Objective
The primary goal was to fix the AI-driven self-healing pipeline where the Airflow DAG automatically runs `gemini.py` after a step (like `syn_debug`), generates a patch for errors, and branches the pipeline to retry.

## the Journey & Iterations

### Attempt 1: Git Diff + DAG Branching via TriggerDagRun
**Approach:** 
We originally tried to have the AI output a precise unified `git diff` format, which would then be validated with `git apply --check` and applied with `git apply`.
Whenever a patch was generated, the DAG would use `TriggerDagRunOperator` to spawn an entirely cloned sub-DAG to run the patched flow in parallel.

**Why it failed:**
1. **Airflow Environment Issues:** `subprocess.run("python3 gemini.py")` was failing silently because Airflow workers didn't source the Conda bashrc properly, meaning `google-generativeai` and the API key were missing.
2. **Silent Failures:** Using `check=True` in `subprocess.run()` suppressed `stderr` from going to the Airflow logs, making debugging extremely difficult when the script crashed.
3. **AI Diff Hallucinations:** The LLM consistently hallucinated line numbers in `@@ -old,count +new,count @@` hunks. It also added incorrect indentation and mismatched the `+`/`-` line spacing. `git apply --check` enforces extreme strictness, so patches were rejected constantly.
4. **Incorrect File Paths:** The prompt was written such that the AI thought it was writing to relative files like `--- a/syn.yml` instead of the deeply nested `configs-design/gcd/syn.yml`.

### Attempt 2: Cascading Patch Application
**Approach:**
We rewrote `patcher.py` to first attempt `git apply`. If that failed, we wrote custom python logic to parse the `git diff` blocks ourselves, resolve the file's basename to its absolute location by walking the directory tree, and apply the hunk changes using fuzzy algorithms.

**Why it failed (partially):**
While the path resolution (`_find_file`) was upgraded to intelligently search `configs-design/` and `src/`, pulling hunks out of a hallucinated `git diff` was still too fragile. The LLM's broken unified-diff syntax often broke the manual parser as well.

### Attempt 3 (Final Solution): Exact Find-and-Replace
**Approach:**
We abandoned `git diff` entirely. Instead, we updated the `config.yml` prompts to instruct the AI to use a simple schema based purely on finding exact text and replacing it:
```
FILE: [basename]
FIND:
[exact copy-pasted text from the current code]
REPLACE_WITH:
[new text]
END_PATCH
```
**Why it succeeded:**
1. LLMs excel at exact text regurgitation instead of calculating line-number differentials.
2. We updated `patcher.py` to use a native Python `content.replace(find_text, replace_text, 1)`.
3. We added a fallback fuzzy-matcher that ignores trailing whitespace and normalizes indentation just in case the AI messes up the spacing.
4. All patched files are safely backed up to an `archive_dir/originals/` folder before they are touched.
5. In-place modification allows Hammer to seamlessly pick up the updated configs in the next step.

### Reverting the DAG Architecture
**Context:**
During Attempt 1, we introduced `TriggerDagRunOperator` into `hammer_vlsi.py` to create recursive pipeline branches (`branch_20260305_syn`). 

**Correction:**
We realized this architectural drift deviated entirely from the upstream `juhyundo/hammer_dep (airflow_dev)` branch. The user requested we revert the DAG structure back to standard linear progression, but keep the `sim_rtl_debug`, `syn_debug`, and `par_debug` tasks.

**Final DAG State:**
We fetched the raw, original `airflow_dev` code and strategically re-injected the 3 `_debug` tasks without the branching logic.
- Pipeline flows straight through: `sim_rtl >> sim_rtl_debug >> syn >> syn_debug >> par >> par_debug`.
- If an error occurs, the debug task runs the Find-and-Replace patcher to update the code in-place, and the user can manually restart the DAG or next phase from Airflow as needed. No auto-spawned DAG clones.

## Final Environment Fixes Implemented
- Changing `subprocess.run` to call `sys.executable` ensures the script runs in the Airflow conda instance.
- Removing `check=True` and setting `capture_output=True` guarantees both standard out and standard errors are piped to the Airflow logs UI.
- Securely storing the API Key in an `autoTA/.api_key` file (with 600 permissions, tracked in `.gitignore`) resolved all bashrc fallback bugs.
- `.gitignore` was aggressively updated to ignore PyCache, locally-bundled Conda versions, and API keys.
