# Sub-step checkpoints: automatic resume, manual selection, and the database

Genus and innovus write a checkpoint of the design at every step boundary
(`write_db pre_<step>`) while the generated script runs. Upstream hammer can
restart from one, but only if you diagnose which step failed and pass
`--from_step` by hand, and nothing checks that the checkpoint is real or that
your config still matches it. SledgeHammer turns those checkpoints into a
first-class mechanism with three layers:

1. **Automatic resume.** When a syn or par run dies partway, the next plain
   invocation picks up from the last checkpoint the tool confirmed writing.
2. **Validated manual selection.** `--from_step` / `--only_step` (CLI or the
   DAG trigger form) refuse to start from a step whose checkpoint does not
   exist, and tell you what is available instead.
3. **Database checkpoints.** A failed or paused stage pushes its newest
   trusted checkpoint to Postgres, so a fresh checkout, a wiped rundir, or a
   teammate's machine can resume a run it never executed.

## How a checkpoint earns trust

A checkpoint is only used when all of these hold:

1. The tool's own log confirms the write finished. Genus prints "Finished
   exporting design database to file 'pre_X'". Innovus never prints a
   completion line, so trust comes from the `latest` symlink (the script
   repoints it immediately after each `write_db` returns) plus the ordering
   of "Writing Binary DB to pre_X/" lines: a later write starting proves the
   earlier one finished. A write the tool died inside of is never loaded.
   Confirmations are collected across all log rotations, so an attempt that
   died before confirming anything does not erase the proof from earlier
   attempts. The resume point is therefore the deepest trusted progress for
   the current inputs, which can be deeper than where the last attempt died
   or paused.
2. The inputs haven't changed. A marker file in the rundir records the stage
   key (the same config+RTL fingerprint the PD cache uses) for the attempt
   that produced the checkpoints. Any config or RTL change means the
   checkpoints describe a different design: they are deleted and the run
   starts from scratch (after checking the database for a checkpoint pushed
   under the new key, e.g. from another machine).
3. It hasn't been burned. If a resume makes no progress (no new confirmed
   checkpoint), that rung is burned and the next attempt uses the next older
   one, ending at a scratch run. The `read_db` in a resume script is guarded
   (`catch` + exit) because both tools otherwise drop to an interactive
   prompt on a bad database and hang the run forever: a corrupt checkpoint
   now costs one failed attempt of about half a minute, never a hang and
   never a loop.

One clamp per tool: a resume never starts later than `write_regs`, because
the output bookkeeping needs the tail steps to run in the current invocation.
Those tail steps cost seconds.

## Database checkpoints

Every failure exit of a stage (tool crash, deliberate pause, error-scan
policy failure) pushes the newest trusted checkpoint to Postgres: the
`pre_<step>` file (genus) or directory (innovus), compressed, keyed by the
stage key and step, with design / module / owner / DAG / project provenance.
When the stage later commits successfully its rows are deleted, so the table
only ever holds stages that are currently broken or paused. That makes
`studio checkpoints` a live answer to "what is resumable right now".

Restores prefer local files and fall back to the database. The fallback
covers three cases: an empty rundir (fresh checkout, wiped workspace, another
machine), a key mismatch where someone else pushed a checkpoint for the
current config, and a burned-out local ladder. A checkpoint that already
failed to make progress is not fetched twice.

Commands and switches:

- `studio checkpoints` lists stored checkpoints (`--design`, `--stage`,
  `--key`, `--keys` to print stage keys).
- `studio checkpoints-push --rundir <dir> --stage syn|par` uploads a local
  checkpoint by hand (bank partial progress before wiping a machine, or hand
  a paused run to a teammate). Trust rules still apply: only tool-confirmed
  checkpoints push, the stage key comes from the rundir's marker.
- `studio checkpoints-fetch --id N --dest <rundir>` downloads any row as
  `pre_<step>`, with no key check: an explicit choice, like naming a step
  with `--from_step`. This is the manual bridge for continuing a run whose
  config has since changed; it prints the exact `--from_step` to use next.
- Clearing is automatic and follows the flow: the rundir's marker remembers
  every config key it attempted, and a successful run deletes the rows for
  its current key plus that whole lineage. Completing a stage after a config
  fix therefore sweeps the rows its own broken predecessors pushed, while a
  teammate debugging a different config of the same design keeps their row
  (their keys are not in your rundir's lineage). `studio checkpoints-clear`
  (`--id`, `--key`, `--design`, `--older-than-days`) remains for rundirs
  that were abandoned outright.
- Off switch: `HAMMER_DB_CHECKPOINTS=0` or
  `vlsi.substep_resume.db_checkpoints: false`. Local resume keeps working.

## Choosing a step by hand

Explicit step flags always win over automatic resume, and they now mean what
they say:

- `--from_step X` / `--only_step X` first verify that `pre_X` exists, locally
  or in the database (a database hit is downloaded into the rundir
  automatically). If it exists nowhere, the run refuses in seconds with the
  list of steps that are available locally and in the database, instead of
  wedging the tool on a checkpoint that was never written.
- Explicit step flags bypass the unchanged-inputs skip. Previously a
  `--from_step` on an already-committed stage silently did nothing in two
  seconds; now asking for a step means the stage runs.
- `--force` still means a scratch run, and a whole-stage cache HIT still
  beats resume when no step flags are given: if the finished result is in
  the cache there is nothing to resume.

Note the checkpoint a step flag needs is the boundary written before that
step, so after a pause at `add_tieoffs` the resumable step is the next one
whose boundary got written. `studio checkpoints` or the refusal message will
tell you the exact names.

## Pausing a stage

`--to_step X` stops the stage after step X. Two fixes make this usable:

- Genus actually runs. Its tool launch used to be the last step of the list,
  so stopping earlier emitted a truncated script and never started the tool.
  The launch now happens even when flow control stops before it.
- A paused run is treated as deliberately incomplete rather than as success
  or as a mystery error. The selected steps execute and checkpoint (and push
  to the database), but the stage does not commit: no output json, the
  dependency state stays needs-rerun, and the exit code is nonzero so make
  and DAG tasks cannot treat a half-run stage as done. The log says exactly
  what to do: "Checkpoints up to the stop step are saved. Rerun without step
  flags to auto-resume from there."

The pause/inspect/continue loop is therefore: `--to_step X`, look at
whatever you wanted to look at, plain rerun to continue.

## Time accounting

A successful resume records a `RESUME` event in the time-saved ledger. Its
`saved_seconds` is measured, not estimated: the span of the previous
attempt's checkpoint timestamps covering the steps that were skipped
(unknown, and left null, when the checkpoint came from the database). The
report shows it as "Saved by substep resume" and counts it in SLEDGEHAMMER
TIME SAVED. Legacy hammer could achieve the same skip with a hand-written
`--from_step`, so quote this bucket separately if a reviewer pushes back;
the automation, validation, and cross-machine restore are what the fork adds.

## On the DAG

Automatic resume needs nothing from the UI: DAG tasks run the same CLI path,
so a task that dies mid-tool resumes on the next triggered run by itself
(the `redo` checkbox forces a scratch run instead). Manual step selection is
in the trigger form, injected straight into the task's argv:

- `From step` / `To step` / `Only step` - free-text sub-step names, validated
  the same way as the CLI flags.
- `Step flags apply to` - which stage the fields target (`syn` or `par`), so
  a from_step meant for syn can never leak into par or a bridge action.

Example: rerun just the tieoff insertion of a hierarchical module: select
`syn`, Modules `RocketTile`, From step `add_tieoffs`.

## Verified behavior

All of this was tested live against the real Postgres instance, not just
unit-checked (12 genus + 6 innovus + 10 database unit checks also pass):

- riscv151 syn (demo_riscv, sky130): killed mid-run, resumed, completed;
  the resumed netlist was byte-identical to a scratch run. Double-kill
  chains resume from the later checkpoint. A corrupt checkpoint failed in
  24 seconds, burned its rung, and the next run stepped down and completed.
- riscv151 par (innovus 25.11, python mode): killed 60 seconds into routing,
  resumed from `route_opt_design`, produced the final GDS. In python mode
  the evidence is in `par.py` (`read_db('pre_X')`); the log does not echo
  script commands.
- RocketConfig (tstech16c, hierarchical `syn-RocketTile`): killed 33 minutes
  in, resumed from `syn_map`, netlist byte-identical to the known-good run,
  ledger recorded RESUME saved=1822s. A stale output json from an old
  successful run in the same build dir did not block the resume.
- Database round trip: a paused stage pushed a compressed checkpoint; the
  rundir was deleted entirely; the next plain run fetched the checkpoint
  from Postgres, resumed, completed, and cleared the row. A manual
  `--from_step` with the checkpoint only in the database downloaded and used
  it. A bogus step name was refused in 4 seconds with the availability list.

## Scope and known gaps

- Syn (genus) and par (innovus) are covered. Other tools with checkpointing
  (tempus, joules, conformal, openroad) would follow the same per-tool
  confirm-pattern + ceiling recipe.
- A paused or crashed task shows as a red FAILED task in the Airflow UI even
  though a pause is intentional. Mapping the pause to Airflow's SKIPPED
  state (distinct exit code + AirflowSkipException in the task wrapper) is a
  small planned improvement.
- Config changes invalidate checkpoints by design. After you fix a config or
  hook, automatic resume goes scratch; the supported debug loop is manual
  `--from_step` on the same machine (local checkpoints are accepted by
  presence, since naming a step is taking responsibility). An opt-in
  `allow_config_change` for automatic and cross-machine resume across a fix
  is designed but deliberately not built yet.
- If a run SUCCEEDED and you then delete its outputs with the cache off, the
  dependency check skips without re-running and nothing regenerates the
  outputs (the pre-existing SKIP_NO_BLOB hole). With the cache on,
  SKIP_RESTORED already covers this. Wiring resume into that skip path is
  future work.
