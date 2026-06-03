"""
hammer-pd-store: CLI for the Postgres-backed PD store.

Roughly grouped by what each subcommand is for:

Schema and JSON-artifact operations (from the original POC):
    init              Create the hammer_poc schema and tables. Idempotent.
    list [-n N]       Show the most recent JSON artifacts.
    get  <sha256>     Print one artifact to stdout.
    put  <path>       Store a JSON file. Prints its sha256.

Master-database and per-stage cache blobs:
    master-push <design> [--master <path>]
    master-pull <design> [--out <path>]
    master-list [-n N]               (list master_database rows with provenance)
    stage-key   <stage_tag> [--master <path>]
    stage-push  <stage_tag> --rundir <path> [--master <path>]
    stage-pull  <stage_tag> --rundir <path> [--master <path>]
    blob-list   [--stage <tag>] [-n N]

Per-user workspaces (used by the shared Airflow + LDAP deployment so that
one user's "clean" task can't wipe another user's build dir):
    workspace-list                  List every registered workspace.
    workspace-show  <username>      Print one user's workspace root.
    workspace-set   <username> <p>  Set or update a user's workspace.
    workspace-unset <username>      Drop a user's registration.

Cache wipes (destructive; default behavior prompts for confirmation):
    wipe-blobs     [--stage <tag>]   Delete stage tarballs. Forces cold run.
    wipe-master    [--design <name>] Delete dep-check baseline.
    wipe-artifacts [--kind <kind>]   Delete JSON artifacts (par-input, etc.).
    wipe-all                         Delete all cache. user_workspaces is kept.

Design registration (turn RTL into Hammer configs without hand-writing YAML):
    design-register --name <n> --top-module <m> --rtl <paths...> --clock-ns <c>
    augment --design <n>            (fill in SRAM stuff for an existing design)
    make-dag --design <n>           (generate the Airflow DAG and link it in)

`<path>` defaults to ./master_database.json when omitted.

Connection settings come from HAMMER_PG_* env vars, or fall back to
sql_alchemy_conn in airflow.cfg. See hammer/vlsi/pd_store.py for the
precise resolution order.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional

from hammer.config import HammerJSONEncoder
from hammer.vlsi import pd_store


def _cmd_init(_args: argparse.Namespace) -> int:
    pd_store.ensure_schema()
    print(f"Initialized schema '{pd_store.SCHEMA_NAME}' and table '{pd_store.TABLE_NAME}'.")
    return 0


def _short_str(s: Optional[str], n: int) -> str:
    if s is None:
        return "-"
    s = str(s)
    return s if len(s) <= n else s[:n - 1] + "…"


def _cmd_list(args: argparse.Namespace) -> int:
    rows = pd_store.list_artifacts(limit=args.limit)
    if not rows:
        print("(no artifacts)")
        return 0
    header = (f"{'sha':<10} {'kind':<11} {'top_module':<18} {'owner':<14} "
              f"{'trig_user':<14} {'design':<28} {'dag_id':<22}  created_at")
    print(header)
    print("-" * len(header))
    for r in rows:
        sha, kind, top_module, owner, trig, dag, design, workspace, created = r
        print(f"{_short_str(sha,10):<10} {_short_str(kind,11):<11} "
              f"{_short_str(top_module,18):<18} {_short_str(owner,14):<14} "
              f"{_short_str(trig,14):<14} {_short_str(design,28):<28} "
              f"{_short_str(dag,22):<22}  {created}")
    return 0


def _cmd_master_list(args: argparse.Namespace) -> int:
    rows = pd_store.list_master_databases(limit=args.limit)
    if not rows:
        print("(no master_databases)")
        return 0
    header = (f"{'design':<32} {'owner':<14} {'trig_user':<14} "
              f"{'dag_id':<28} {'workspace':<48}  updated_at")
    print(header)
    print("-" * len(header))
    for r in rows:
        design, owner, trig, dag, workspace, updated = r
        print(f"{_short_str(design,32):<32} {_short_str(owner,14):<14} "
              f"{_short_str(trig,14):<14} {_short_str(dag,28):<28} "
              f"{_short_str(workspace,48):<48}  {updated}")
    return 0


def _cmd_get(args: argparse.Namespace) -> int:
    data = pd_store.load_artifact(args.sha256)
    if data is None:
        print(f"No artifact found with sha256={args.sha256}", file=sys.stderr)
        return 1
    print(json.dumps(data, cls=HammerJSONEncoder, indent=4))
    return 0


def _cmd_put(args: argparse.Namespace) -> int:
    with open(args.path, "r") as f:
        data = json.load(f)
    sha = pd_store.store_artifact(data, kind=args.kind)
    print(sha)
    return 0


def _read_master(path: Optional[str]) -> dict:
    p = Path(path) if path else Path("master_database.json")
    if not p.is_file():
        print(f"master_database not found at {p}", file=sys.stderr)
        sys.exit(2)
    with p.open("r") as f:
        return json.load(f)


def _cmd_master_push(args: argparse.Namespace) -> int:
    master_db = _read_master(args.master)
    pd_store.store_master_database(args.design, master_db)
    print(f"Pushed master_database for design '{args.design}'.")
    return 0


def _cmd_master_pull(args: argparse.Namespace) -> int:
    db = pd_store.load_master_database(args.design)
    if db is None:
        print(f"No master_database found for design '{args.design}'.", file=sys.stderr)
        return 1
    payload = json.dumps(db, cls=HammerJSONEncoder, indent=4, sort_keys=True)
    if args.out:
        Path(args.out).write_text(payload)
        print(f"Wrote {args.out}")
    else:
        print(payload)
    return 0


def _cmd_stage_key(args: argparse.Namespace) -> int:
    master_db = _read_master(args.master)
    print(pd_store.compute_stage_key(master_db, args.stage))
    return 0


def _cmd_stage_push(args: argparse.Namespace) -> int:
    master_db = _read_master(args.master)
    rundir = Path(args.rundir)
    if not rundir.is_dir():
        print(f"rundir not found: {rundir}", file=sys.stderr)
        return 2
    sha = pd_store.compute_stage_key(master_db, args.stage)
    data = pd_store.tar_directory(rundir)
    pd_store.store_stage_blob(args.stage, sha, data)
    print(f"sha256={sha} stage={args.stage} bytes={len(data)} from={rundir}")
    return 0


def _cmd_stage_pull(args: argparse.Namespace) -> int:
    master_db = _read_master(args.master)
    sha = pd_store.compute_stage_key(master_db, args.stage)
    blob = pd_store.load_stage_blob(sha)
    if blob is None:
        print(f"No blob for sha256={sha} stage={args.stage}", file=sys.stderr)
        return 1
    stored_stage, data, _duration, _cpu = blob
    rundir = Path(args.rundir)
    if rundir.exists():
        if not args.overwrite:
            print(
                f"{rundir} already exists. Pass --overwrite to replace it.",
                file=sys.stderr,
            )
            return 2
        shutil.rmtree(rundir)
    rundir.parent.mkdir(parents=True, exist_ok=True)
    pd_store.untar_to_directory(data, rundir.parent)
    print(f"sha256={sha} stage={stored_stage} extracted to {rundir}")
    return 0


def _cmd_grant(args: argparse.Namespace) -> int:
    pd_store.grant_access(args.role)
    print(f"Added '{args.role}' to {pd_store.SLEDGEHAMMER_GROUP}.")
    return 0


def _cmd_revoke(args: argparse.Namespace) -> int:
    pd_store.revoke_access(args.role)
    print(f"Removed '{args.role}' from {pd_store.SLEDGEHAMMER_GROUP}.")
    return 0


def _confirm(prompt: str, assume_yes: bool) -> bool:
    """Prompt the user to confirm a destructive op. Returns True to proceed."""
    if assume_yes:
        return True
    try:
        reply = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return reply in ("y", "yes")


def _cmd_wipe_blobs(args: argparse.Namespace) -> int:
    target = (
        f"all rows in {pd_store.FQ_BLOB}"
        if not args.stage
        else f"rows in {pd_store.FQ_BLOB} where stage = '{args.stage}'"
    )
    if not _confirm(f"This will permanently delete {target}. Continue?", args.yes):
        print("Aborted.", file=sys.stderr)
        return 1
    n = pd_store.delete_stage_blobs(stage_tag=args.stage)
    print(f"Deleted {n} row(s) from {pd_store.FQ_BLOB}.")
    return 0


def _cmd_wipe_master(args: argparse.Namespace) -> int:
    target = (
        f"all rows in {pd_store.FQ_MASTER}"
        if not args.design
        else f"the row in {pd_store.FQ_MASTER} for design = '{args.design}'"
    )
    if not _confirm(f"This will permanently delete {target}. Continue?", args.yes):
        print("Aborted.", file=sys.stderr)
        return 1
    n = pd_store.delete_master_databases(design=args.design)
    print(f"Deleted {n} row(s) from {pd_store.FQ_MASTER}.")
    return 0


def _cmd_wipe_artifacts(args: argparse.Namespace) -> int:
    target = (
        f"all rows in {pd_store.FQ_TABLE}"
        if not args.kind
        else f"rows in {pd_store.FQ_TABLE} where kind = '{args.kind}'"
    )
    if not _confirm(f"This will permanently delete {target}. Continue?", args.yes):
        print("Aborted.", file=sys.stderr)
        return 1
    n = pd_store.delete_artifacts(kind=args.kind)
    print(f"Deleted {n} row(s) from {pd_store.FQ_TABLE}.")
    return 0


def _cmd_wipe_all(args: argparse.Namespace) -> int:
    # Note: user_workspaces is intentionally left alone. That's config (where
    # each user's builds live), not cache state, and clobbering it would force
    # every user to re-register on next login.
    target = (
        f"ALL cache data: pd_blobs + master_databases + pd_artifacts in "
        f"schema {pd_store.SCHEMA_NAME}. user_workspaces is preserved."
    )
    if not _confirm(f"This will permanently delete {target}. Continue?", args.yes):
        print("Aborted.", file=sys.stderr)
        return 1
    blobs = pd_store.delete_stage_blobs()
    master = pd_store.delete_master_databases()
    artifacts = pd_store.delete_artifacts()
    print(f"Deleted {blobs} row(s) from {pd_store.FQ_BLOB}.")
    print(f"Deleted {master} row(s) from {pd_store.FQ_MASTER}.")
    print(f"Deleted {artifacts} row(s) from {pd_store.FQ_TABLE}.")
    return 0


def _cmd_design_register(args: argparse.Namespace) -> int:
    """
    Walk a directory of RTL and write out the Hammer configs for the design.

    See hammer/shell/design_register.py for the actual work. This wrapper
    just validates arg paths, hands them off, then prints the warning list
    and the next steps the user still has to do by hand.
    """
    from hammer.shell import design_register
    rtl_paths = [Path(p) for p in args.rtl]
    for p in rtl_paths:
        if not p.exists():
            print(f"RTL path not found: {p}", file=sys.stderr)
            return 2
    out_dir, warnings = design_register.register_design(
        name=args.name,
        top_module=args.top_module,
        clock_ns=args.clock_ns,
        rtl_paths=rtl_paths,
        pdk=args.pdk,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        exclude_patterns=args.exclude,
        use_default_excludes=not args.no_default_excludes,
    )
    if warnings:
        print("", file=sys.stderr)
        print("WARNINGS:", file=sys.stderr)
        for w in warnings:
            print(f"  - {w}", file=sys.stderr)
    print(f"\nDesign registered. Configs at {out_dir}.", file=sys.stderr)
    print(f"Next steps:", file=sys.stderr)
    print(f"  1. Edit {out_dir}/par.yml to add macro placement constraints.", file=sys.stderr)
    print(f"  2. Write a DAG file for this design (no factory yet, so still a copy-paste).", file=sys.stderr)
    print(f"  3. Trigger via the Airflow UI or hammer-vlsi syn_par.", file=sys.stderr)
    return 0


def _cmd_augment(args: argparse.Namespace) -> int:
    """
    Look at a design's existing common.yml, find the SRAM macros, and write
    the blackbox stubs + sky130-extras.yml. Doesn't touch the user's other
    configs.
    """
    from hammer.shell import design_register

    design_dir = Path(args.design_dir) if args.design_dir else None
    if design_dir is None:
        cwd = Path.cwd()
        if (cwd / "e2e").is_dir():
            design_dir = cwd / "e2e" / "configs-design" / args.design
        else:
            design_dir = Path("e2e/configs-design") / args.design

    if not design_dir.is_dir():
        print(f"Design directory not found: {design_dir}", file=sys.stderr)
        print(f"Create it first (with common.yml etc.) or pass --design-dir.",
              file=sys.stderr)
        return 2

    try:
        srams, warnings = design_register.augment_existing_design(design_dir)
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2

    print(f"\nAugmented {design_dir}.", file=sys.stderr)
    print(f"  SRAM macros bound: {len(srams)}", file=sys.stderr)
    if warnings:
        print("\nNotes:", file=sys.stderr)
        for w in warnings:
            print(f"  - {w}", file=sys.stderr)
    return 0


def _cmd_make_dag(args: argparse.Namespace) -> int:
    """
    Build the Airflow DAG for an already-configured design and symlink it
    into the dags/ folder so Airflow's scheduler picks it up.
    """
    import os

    design = args.design
    design_dir = Path(args.design_dir) if args.design_dir else None
    if design_dir is None:
        cwd = Path.cwd()
        if (cwd / "e2e").is_dir():
            design_dir = cwd / "e2e" / "configs-design" / design
        else:
            design_dir = Path("e2e/configs-design") / design

    if not design_dir.is_dir():
        print(f"Design directory not found: {design_dir}", file=sys.stderr)
        return 2

    if not args.skip_augment:
        from hammer.shell import design_register
        try:
            srams, warnings = design_register.augment_existing_design(design_dir)
            print(f"  Augment: bound {len(srams)} SRAM macros", file=sys.stderr)
            for w in warnings:
                print(f"  WARNING: {w}", file=sys.stderr)
        except (FileNotFoundError, ValueError) as e:
            print(f"Augment step failed: {e}", file=sys.stderr)
            print("Pass --skip-augment to bypass.", file=sys.stderr)
            return 2

    repo_root = Path(args.repo_root) if args.repo_root else Path.cwd()
    e2e = repo_root / "e2e"
    obj_dir = Path(args.obj_dir) if args.obj_dir else (
        e2e / f"build-{args.pdk}-{args.tools}" / design
    )
    obj_dir.mkdir(parents=True, exist_ok=True)

    dags_folder = Path(args.dags_folder) if args.dags_folder else (repo_root / "dags")

    env_conf = e2e / "configs-env" / f"{args.env}-env.yml"
    pdk_conf = e2e / "configs-pdk" / f"{args.pdk}.yml"
    tools_dir = e2e / "configs-tool"
    tools_conf = tools_dir / f"{args.tools}.yml"

    # Discover every tool config so the DAG can offer them as a runtime
    # dropdown. The default stays whatever --tools picked.
    design_confs = [str(yml) for yml in sorted(design_dir.glob("*.yml"))]
    proj_confs_by_tools: Dict[str, List[str]] = {}
    for tool_yml in sorted(tools_dir.glob("*.yml")):
        proj_confs_by_tools[tool_yml.stem] = [str(pdk_conf), str(tool_yml)] + design_confs

    # The driver itself needs ONE project_configs list to compute the dep
    # graph (hier vs flat detection, top_module resolution, etc.). Use the
    # caller's chosen --tools.
    proj_confs = proj_confs_by_tools.get(
        args.tools, [str(pdk_conf), str(tools_conf)] + design_confs
    )

    from hammer.vlsi import HammerDriver, HammerDriverOptions
    import hammer.config as hcfg

    opts = HammerDriverOptions(
        environment_configs=[str(env_conf)],
        project_configs=proj_confs,
        log_file=str(obj_dir / "hammer.log"),
        obj_dir=str(obj_dir),
    )
    extra_dict = {
        "vlsi.core.build_system": "airflow",
        "vlsi.core.airflow_dags_folder": str(dags_folder),
    }
    if args.dag_id:
        extra_dict["vlsi.core.airflow_dag_id"] = args.dag_id
    import json
    extra = hcfg.load_config_from_string(json.dumps(extra_dict), is_yaml=False)
    driver = HammerDriver(opts, extra)

    from hammer.vlsi.hammer_build_systems import build_airflow_dag
    errs: List[str] = []
    dep_graph = build_airflow_dag(
        driver, errs.append,
        proj_confs_by_tools=proj_confs_by_tools,
        default_tools=args.tools,
    )
    for e in errs:
        print(f"WARNING: {e}", file=sys.stderr)

    dag_label = args.dag_id or f"Hammer_{design}"
    if dep_graph:
        leaves = sorted(m for m, edges in dep_graph.items() if not edges[1])
        non_leaves = sorted(m for m, edges in dep_graph.items() if edges[1])
        print(f"\nHierarchical flow detected: {len(dep_graph)} modules.",
              file=sys.stderr)
        print(f"  non-leaf: {', '.join(non_leaves) or '(none)'}", file=sys.stderr)
        print(f"  leaves:   {', '.join(leaves) or '(none)'}", file=sys.stderr)
    else:
        print(f"\nFlat flow.", file=sys.stderr)

    tool_names = sorted(proj_confs_by_tools.keys())
    print(f"\nTool dropdown options (default {args.tools!r}): {tool_names}",
          file=sys.stderr)
    print(f"DAG for '{design}' generated. Airflow's dag-processor should",
          file=sys.stderr)
    print(f"register it within ~30 seconds as {dag_label}.", file=sys.stderr)
    return 0


def _cmd_workspace_list(_args: argparse.Namespace) -> int:
    rows = pd_store.list_user_workspaces()
    if not rows:
        print("(no workspaces registered)")
        return 0
    print(f"{'username':<24} {'workspace_root':<60} updated_at")
    print("-" * 120)
    for username, root, updated_at in rows:
        print(f"{username:<24} {root:<60} {updated_at}")
    return 0


def _cmd_workspace_show(args: argparse.Namespace) -> int:
    root = pd_store.get_user_workspace(args.username)
    print(root)
    return 0


def _cmd_workspace_set(args: argparse.Namespace) -> int:
    pd_store.set_user_workspace(args.username, args.workspace_root)
    print(f"Set workspace for '{args.username}' -> {args.workspace_root}")
    return 0


def _cmd_workspace_unset(args: argparse.Namespace) -> int:
    if pd_store.delete_user_workspace(args.username):
        print(f"Removed workspace registration for '{args.username}'.")
        return 0
    print(f"No workspace was registered for '{args.username}'.", file=sys.stderr)
    return 1


def _human_bytes(n: Optional[int]) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _human_dur(s: Optional[float]) -> str:
    if s is None:
        return "-"
    s = float(s)
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{int(s/60)}m{int(s%60):02d}s"
    return f"{int(s/3600)}h{int((s%3600)/60):02d}m"


def _cmd_blob_list(args: argparse.Namespace) -> int:
    rows = pd_store.list_stage_blobs(stage_tag=args.stage, limit=args.limit)
    if not rows:
        print("(no blobs)")
        return 0
    header = (f"{'sha':<10} {'stage':<10} {'size':>8} {'wall':>10} {'cpu':>10} "
              f"{'owner':<14} {'trig_user':<14} {'design':<28} {'dag_id':<28}  created_at")
    print(header)
    print("-" * len(header))
    for r in rows:
        (sha, stage, size, dur_s, cpu_s, owner, trig, dag,
         design, workspace, created) = r
        print(f"{_short_str(sha,10):<10} {_short_str(stage,10):<10} {_human_bytes(size):>8} "
              f"{_human_dur(dur_s):>10} {_human_dur(cpu_s):>10} "
              f"{_short_str(owner,14):<14} "
              f"{_short_str(trig,14):<14} {_short_str(design,28):<28} "
              f"{_short_str(dag,28):<28}  {created}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hammer-pd-store",
        description="Postgres PD artifact store (POC).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Create schema + table if not present.")
    p_init.set_defaults(func=_cmd_init)

    p_list = sub.add_parser("list", help="List recent artifacts.")
    p_list.add_argument("-n", "--limit", type=int, default=20,
                        help="Number of rows to show (default: 20).")
    p_list.set_defaults(func=_cmd_list)

    p_get = sub.add_parser("get", help="Fetch an artifact by SHA256 and print its JSON.")
    p_get.add_argument("sha256", help="SHA256 hex digest of the artifact.")
    p_get.set_defaults(func=_cmd_get)

    p_put = sub.add_parser("put", help="Store a JSON file as an artifact.")
    p_put.add_argument("path", help="Path to the JSON file to store.")
    p_put.add_argument("--kind", default="par-input",
                       help="Artifact kind label (default: par-input).")
    p_put.set_defaults(func=_cmd_put)

    p_mpush = sub.add_parser("master-push", help="Upsert a master_database for a design.")
    p_mpush.add_argument("design", help="Design name to use as the row key.")
    p_mpush.add_argument("--master", default=None,
                         help="Path to master_database.json (default: ./master_database.json).")
    p_mpush.set_defaults(func=_cmd_master_push)

    p_mpull = sub.add_parser("master-pull", help="Fetch a master_database by design.")
    p_mpull.add_argument("design")
    p_mpull.add_argument("--out", default=None,
                         help="Write JSON to this path. Default: stdout.")
    p_mpull.set_defaults(func=_cmd_master_pull)

    p_mlist = sub.add_parser(
        "master-list",
        help="List master_database rows with provenance (design, owner, "
             "triggering_user, dag_id, workspace, updated_at). One row per "
             "design.",
    )
    p_mlist.add_argument("-n", "--limit", type=int, default=50)
    p_mlist.set_defaults(func=_cmd_master_list)

    p_skey = sub.add_parser("stage-key", help="Compute a stage's cache key from a master_database.")
    p_skey.add_argument("stage", choices=pd_store.KNOWN_STAGE_TAGS,
                        help="Stage tag (e.g. 'synthesis', 'par').")
    p_skey.add_argument("--master", default=None,
                        help="Path to master_database.json (default: ./master_database.json).")
    p_skey.set_defaults(func=_cmd_stage_key)

    p_spush = sub.add_parser("stage-push", help="Tar a stage rundir and store it under its cache key.")
    p_spush.add_argument("stage", choices=pd_store.KNOWN_STAGE_TAGS)
    p_spush.add_argument("--rundir", required=True,
                         help="Path to the stage's run directory (e.g. obj_dir/syn-rundir).")
    p_spush.add_argument("--master", default=None,
                         help="Path to master_database.json (default: ./master_database.json).")
    p_spush.set_defaults(func=_cmd_stage_push)

    p_spull = sub.add_parser("stage-pull", help="Fetch a stage tarball by cache key and untar it.")
    p_spull.add_argument("stage", choices=pd_store.KNOWN_STAGE_TAGS)
    p_spull.add_argument("--rundir", required=True,
                         help="Where to extract (e.g. obj_dir/syn-rundir).")
    p_spull.add_argument("--master", default=None,
                         help="Path to master_database.json (default: ./master_database.json).")
    p_spull.add_argument("--overwrite", action="store_true",
                         help="Replace --rundir if it already exists.")
    p_spull.set_defaults(func=_cmd_stage_pull)

    p_blist = sub.add_parser("blob-list", help="List stored stage tarballs.")
    p_blist.add_argument("--stage", default=None, choices=pd_store.KNOWN_STAGE_TAGS)
    p_blist.add_argument("-n", "--limit", type=int, default=20)
    p_blist.set_defaults(func=_cmd_blob_list)

    p_grant = sub.add_parser("grant",
                             help=f"Add a role to the {pd_store.SLEDGEHAMMER_GROUP} group.")
    p_grant.add_argument("role", help="Postgres role name (e.g. 'colin').")
    p_grant.set_defaults(func=_cmd_grant)

    p_revoke = sub.add_parser("revoke",
                              help=f"Remove a role from the {pd_store.SLEDGEHAMMER_GROUP} group.")
    p_revoke.add_argument("role")
    p_revoke.set_defaults(func=_cmd_revoke)

    p_ws_list = sub.add_parser(
        "workspace-list",
        help="List all registered per-user workspace roots.",
    )
    p_ws_list.set_defaults(func=_cmd_workspace_list)

    p_ws_show = sub.add_parser(
        "workspace-show",
        help="Print the workspace root for a user (auto-registers default if missing).",
    )
    p_ws_show.add_argument("username", help="Airflow LDAP username.")
    p_ws_show.set_defaults(func=_cmd_workspace_show)

    p_ws_set = sub.add_parser(
        "workspace-set",
        help="Set or update the workspace root for a user.",
    )
    p_ws_set.add_argument("username", help="Airflow LDAP username.")
    p_ws_set.add_argument(
        "workspace_root",
        help="Absolute path to the user's workspace root. The Airflow daemon "
             "user must have write permission here.",
    )
    p_ws_set.set_defaults(func=_cmd_workspace_set)

    p_ws_unset = sub.add_parser(
        "workspace-unset",
        help="Remove a user's workspace registration. The next call for that "
             "user will auto-register a fresh default.",
    )
    p_ws_unset.add_argument("username")
    p_ws_unset.set_defaults(func=_cmd_workspace_unset)

    # ---- destructive cache-wiping subcommands ----
    p_wb = sub.add_parser(
        "wipe-blobs",
        help="Delete stage tarballs from pd_blobs. Forces a fresh cold run "
             "the next time a stage is invoked. Requires --yes or interactive "
             "confirmation.",
    )
    p_wb.add_argument(
        "--stage",
        choices=pd_store.KNOWN_STAGE_TAGS,
        help="Only delete blobs for this stage (default: ALL stages).",
    )
    p_wb.add_argument("--yes", action="store_true",
                      help="Skip the confirmation prompt.")
    p_wb.set_defaults(func=_cmd_wipe_blobs)

    p_wm = sub.add_parser(
        "wipe-master",
        help="Delete master_databases rows. Forces stage_change_check to say "
             "'run' the next time (no baseline to compare against).",
    )
    p_wm.add_argument(
        "--design",
        help="Only delete the row for this design (default: ALL designs).",
    )
    p_wm.add_argument("--yes", action="store_true",
                      help="Skip the confirmation prompt.")
    p_wm.set_defaults(func=_cmd_wipe_master)

    p_wa = sub.add_parser(
        "wipe-artifacts",
        help="Delete JSON artifacts from pd_artifacts (par-input, etc.).",
    )
    p_wa.add_argument(
        "--kind",
        help="Only delete artifacts of this kind (default: ALL kinds).",
    )
    p_wa.add_argument("--yes", action="store_true",
                      help="Skip the confirmation prompt.")
    p_wa.set_defaults(func=_cmd_wipe_artifacts)

    p_wall = sub.add_parser(
        "wipe-all",
        help="Nuclear: delete pd_blobs + master_databases + pd_artifacts. "
             "Preserves user_workspaces (that's config, not cache).",
    )
    p_wall.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt.")
    p_wall.set_defaults(func=_cmd_wipe_all)

    p_dreg = sub.add_parser(
        "design-register",
        help="Walk a directory of RTL and write Hammer configs for the "
             "design: common.yml, syn.yml, sky130.yml, par.yml, plus a "
             "blackbox stub for every sram22 macro the RTL instantiates. "
             "The sram22 LEF/lib/GDS paths get wired into sky130.yml so "
             "Innovus can find them at place_inst time.",
    )
    p_dreg.add_argument("--name", required=True,
                        help="Design name. The configs end up at "
                             "e2e/configs-design/<name>/.")
    p_dreg.add_argument("--top-module", required=True,
                        help="Top Verilog module. Make sure this matches the "
                             "module name in the RTL, not the file name.")
    p_dreg.add_argument("--clock-ns", required=True, type=float,
                        help="Target clock period in nanoseconds (e.g. 11.8 "
                             "for ~85 MHz).")
    p_dreg.add_argument("--rtl", nargs="+", required=True,
                        help="One or more RTL files or directories. Directories "
                             "get walked recursively for .v and .sv files.")
    p_dreg.add_argument("--pdk", default="sky130", choices=["sky130"],
                        help="PDK to target. sky130 is the only one wired up "
                             "today; asap7 and intech22 are on the to-do list.")
    p_dreg.add_argument("--out-dir", default=None,
                        help="Where to write the configs. Defaults to "
                             "./e2e/configs-design/<name>.")
    p_dreg.add_argument("--exclude", action="append", default=[],
                        help="Glob pattern to skip when walking the RTL "
                             "directory. Repeatable. Stacks on top of the "
                             "built-in pattern list (testbenches, copies, "
                             "etc.); use --no-default-excludes to turn that "
                             "list off.")
    p_dreg.add_argument("--no-default-excludes", action="store_true",
                        help="Turn off the built-in exclude list. Use this if "
                             "your design genuinely needs files matching "
                             "*Testbench.v, *_tb.v, etc. (uncommon).")
    p_dreg.set_defaults(func=_cmd_design_register)

    p_aug = sub.add_parser(
        "augment",
        help="Look at an already-configured design (configs-design/<name>/ "
             "with common.yml etc. in place) and fill in just the SRAM "
             "pieces: blackbox stubs + sky130-extras.yml with extra_libraries. "
             "Doesn't touch the user's other configs. Use this when you've "
             "written your own syn/par/sky130 yml by hand and just want the "
             "macro plumbing automated.",
    )
    p_aug.add_argument("--design", required=True,
                       help="Design name (looks under e2e/configs-design/<name>/).")
    p_aug.add_argument("--design-dir", default=None,
                       help="Override the design directory path.")
    p_aug.set_defaults(func=_cmd_augment)

    p_dag = sub.add_parser(
        "make-dag",
        help="Generate an Airflow DAG for an existing design and symlink it "
             "into the dags/ folder. Wraps hammer-vlsi build with "
             "vlsi.core.build_system=airflow. Within ~30 seconds the DAG "
             "appears as Hammer_<design> in the Airflow UI.",
    )
    p_dag.add_argument("--design", required=True,
                       help="Design name (configs-design/<name>/).")
    p_dag.add_argument("--design-dir", default=None,
                       help="Override the design directory path.")
    p_dag.add_argument("--repo-root", default=None,
                       help="Hammer repo root (defaults to CWD).")
    p_dag.add_argument("--obj-dir", default=None,
                       help="Where to write the generated DAG. "
                            "Defaults to build-<pdk>-<tools>/<design> under e2e/.")
    p_dag.add_argument("--dags-folder", default=None,
                       help="Where to symlink the DAG. Defaults to <repo>/dags.")
    p_dag.add_argument("--pdk", default="sky130")
    p_dag.add_argument("--tools", default="cm")
    p_dag.add_argument("--env", default="bwrc")
    p_dag.add_argument("--dag-id", default=None,
                       help="DAG ID to use in Airflow. Defaults to "
                            "Hammer_<design>. Pick whatever name you want "
                            "shown in the Airflow UI.")
    p_dag.add_argument("--skip-augment", action="store_true",
                       help="Don't run the augment step first. By default "
                            "make-dag runs augment to keep blackbox stubs "
                            "and sky130-extras.yml up to date before "
                            "generating the DAG.")
    p_dag.set_defaults(func=_cmd_make_dag)

    return parser


def main(argv: List[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
