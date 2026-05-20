"""
pd_store_demo: a simple Airflow DAG to show that the Postgres PD artifact
store works end to end, with everything visible in the Airflow UI.

Tasks:
    1. put_sample
       Writes a small par-input-style JSON dict to hammer_poc.pd_artifacts
       and pushes the SHA256 to XCom.

    2. readback_and_verify
       Pulls the SHA from XCom, reads the row back from Postgres,
       and checks that the data matches what was written.

Connection behavior:
    pd_store looks up connection details in this order:
      1. HAMMER_PG_* environment variables (if set)
      2. sql_alchemy_conn in airflow.cfg
      3. Fallback defaults

    If sql_alchemy_conn is already configured, you shouldn’t need to set
    anything else.

On success:
    Both tasks should show as green in the UI. Logs will include:
      - "Stored par-input in Postgres with sha256=<hex>"
      - "READBACK OK: round-trip dict equals original"
"""

from __future__ import annotations

from datetime import datetime

try:
    from airflow.sdk import dag, task
except ImportError:
    from airflow.decorators import dag, task


SAMPLE_PAR_INPUT = {
    "par.inputs.top_module": "gcd",
    "par.inputs.input_files": ["build/syn-gcd/gcd.v"],
    "par.inputs.post_synth_sdc": "build/syn-gcd/gcd.mapped.sdc",
    "vlsi.inputs.clocks": [
        {"name": "clock", "period": "4ns", "uncertainty": "0.1ns"}
    ],
    "vlsi.inputs.placement_constraints": [],
    "pd_store_demo.note": "written by the pd_store_demo DAG",
}


@dag(
    dag_id="pd_store_demo",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["pd_store", "postgres", "poc"],
    description="Round-trip write+read of a PD artifact through hammer_poc.pd_artifacts.",
)
def pd_store_demo():

    @task
    def put_sample() -> str:
        from hammer.vlsi.pd_store import store_par_input, compute_sha256

        expected_sha = compute_sha256(SAMPLE_PAR_INPUT)
        print(f"Expected SHA256 (computed locally): {expected_sha}")

        sha = store_par_input(SAMPLE_PAR_INPUT)
        print(f"Stored par-input in Postgres with sha256={sha}")

        assert sha == expected_sha, (
            f"Mismatch: store returned {sha}, but local compute gave {expected_sha}"
        )
        return sha

    @task
    def readback_and_verify(sha: str) -> None:
        from hammer.vlsi.pd_store import load_artifact, compute_sha256

        print(f"Reading back sha256={sha}")
        got = load_artifact(sha)
        if got is None:
            raise RuntimeError(
                f"No row found for sha256={sha}. Did put_sample actually insert?"
            )

        assert got == SAMPLE_PAR_INPUT, (
            "Round-trip dict does not equal the original payload.\n"
            f"  original keys: {sorted(SAMPLE_PAR_INPUT)}\n"
            f"  readback keys: {sorted(got)}"
        )

        rehash = compute_sha256(got)
        assert rehash == sha, (
            f"Round-trip hash drift: stored={sha}, readback-hash={rehash}"
        )

        print("READBACK OK: round-trip dict equals original")
        print(f"READBACK OK: readback hash matches stored hash ({sha})")

    sha = put_sample()
    readback_and_verify(sha)


pd_store_demo()
