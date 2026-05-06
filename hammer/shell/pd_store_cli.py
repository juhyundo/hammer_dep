"""
hammer-pd-store: CLI for the Postgres-backed PD store.

JSON artifact subcommands (legacy POC):
    init              Create schema + tables (idempotent).
    list [-n N]       List recent JSON artifacts.
    get  <sha256>     Print a JSON artifact to stdout.
    put  <path>       Store a JSON file as an artifact. Prints its SHA256.

Master_database + per-stage blob subcommands:
    master-push <design> [--master <path>]
    master-pull <design> [--out <path>]
    stage-key   <stage_tag> [--master <path>]
    stage-push  <stage_tag> --rundir <path> [--master <path>]
    stage-pull  <stage_tag> --rundir <path> [--master <path>]
    blob-list   [--stage <tag>] [-n N]

`<path>` defaults to ./master_database.json when omitted.

Connection settings come from HAMMER_PG_* env vars or airflow.cfg; see
hammer/vlsi/pd_store.py.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import List, Optional

from hammer.config import HammerJSONEncoder
from hammer.vlsi import pd_store


def _cmd_init(_args: argparse.Namespace) -> int:
    pd_store.ensure_schema()
    print(f"Initialized schema '{pd_store.SCHEMA_NAME}' and table '{pd_store.TABLE_NAME}'.")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    rows = pd_store.list_artifacts(limit=args.limit)
    if not rows:
        print("(no artifacts)")
        return 0
    print(f"{'sha256':<66} {'kind':<12} {'top_module':<24} created_at")
    print("-" * 130)
    for sha256, kind, top_module, created_at in rows:
        tm = top_module or "-"
        print(f"{sha256:<66} {kind:<12} {tm:<24} {created_at}")
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
    stored_stage, data = blob
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


def _cmd_blob_list(args: argparse.Namespace) -> int:
    rows = pd_store.list_stage_blobs(stage_tag=args.stage, limit=args.limit)
    if not rows:
        print("(no blobs)")
        return 0
    print(f"{'sha256':<66} {'stage':<14} {'size':>12}  created_at")
    print("-" * 120)
    for sha, stage, size, created in rows:
        print(f"{sha:<66} {stage:<14} {size:>12}  {created}")
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

    return parser


def main(argv: List[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
