#  hammer_build_systems.py
#  Class containing all the methods to create VLSI flow build system infrastructure
#
#  See LICENSE for licence details.

from .driver import HammerDriver

import os
import sys
import textwrap
from typing import List, Dict, Tuple, Callable

def build_noop(driver: HammerDriver, append_error_func: Callable[[str], None]) -> dict:
    dependency_graph = driver.get_hierarchical_dependency_graph()
    return dependency_graph


def build_makefile(driver: HammerDriver, append_error_func: Callable[[str], None]) -> dict:
    dependency_graph = driver.get_hierarchical_dependency_graph()
    dag_file = os.path.join(driver.obj_dir, "hammer_dag.py")
    
    env_confs = [os.path.realpath(x) for x in driver.options.environment_configs]
    proj_confs = [os.path.realpath(x) for x in driver.options.project_configs]
    obj_dir = os.path.realpath(driver.obj_dir)
    hammer_exec = os.path.realpath(sys.argv[0])

    env_str = ", ".join([f"'{x}'" for x in env_confs])
    proj_str = ", ".join([f"'{x}'" for x in proj_confs])

    # 1. Base DAG Header & Safe Execution Subprocess Wrapper
    output = textwrap.dedent(f"""\
        # Auto-generated Airflow DAG by Hammer Build System
        import os
        import sys
        import pendulum
        import subprocess
        from datetime import datetime, timedelta
        from airflow.decorators import task, dag
        from airflow.models import Param
        from airflow.utils.task_group import TaskGroup
        from airflow.utils.trigger_rule import TriggerRule
        from airflow.exceptions import AirflowSkipException, AirflowFailException

        HAMMER_EXEC = "{hammer_exec}"
        OBJ_DIR = "{obj_dir}"
        ENV_CONFIGS = [{env_str}]
        PROJ_CONFIGS = [{proj_str}]

        default_args = {{
            'owner': 'hammer',
            'start_date': pendulum.datetime(2026, 1, 1, tz="UTC"),
            'retries': 0,
        }}

        def run_hammer_action(action, extra_flags=None):
            \"\"\"
            Spawns a clean python subprocess executing the target stage with explicit manifest pathing.
            \"\"\"
            action_clean = str(action).strip()
            if action_clean.endswith("None"):
                action_clean = action_clean[:-4]

            print(f"Running active Hammer Action: {{action_clean}}")
            
            cmd = [HAMMER_EXEC]
            for env in ENV_CONFIGS:
                cmd += ["-e", env]
            
            # If extra_flags handles downstream context pass-through mapping, use them.
            # Otherwise, default to base project configuration collections.
            has_explicit_project_inputs = False
            if extra_flags:
                for flag in extra_flags:
                    if flag == "-p" or flag == "--project_config":
                        has_explicit_project_inputs = True
                        break
            
            if not has_explicit_project_inputs:
                for proj in PROJ_CONFIGS:
                    cmd += ["-p", proj]
                    
            if extra_flags and str(extra_flags) != "None":
                cmd += extra_flags
                
            cmd += ["--obj_dir", OBJ_DIR, action_clean]
            
            print(f"Executing Process Command: {{' '.join(cmd)}}")
            res = subprocess.run(cmd)
            if res.returncode != 0:
                raise AirflowFailException(f"Hammer action {{action_clean}} failed with exit code {{res.returncode}}")

        def should_run_stage(stage_key, context):
            \"\"\"
            Determines if a stage should execute based on whether it was explicitly 
            selected OR if any downstream dependent tasks are active.
            \"\"\"
            conf = context['dag_run'].conf
            if conf.get(stage_key, False):
                return True
            
            def check_downstream_active(task_obj):
                for downstream in task_obj.downstream_list:
                    downstream_id = downstream.task_id.split('.')[-1]
                    if conf.get(downstream_id, False):
                        return True
                    if check_downstream_active(downstream):
                        return True
                return False

            return check_downstream_active(context['task'])
    """)

    # 2. Dynamic Python Task Generator Logic
    output += textwrap.dedent("""
        @task
        def sim_rtl(suffix, p_sim_rtl_in, sim_rtl_run_dir, **context):
            if should_run_stage('sim_rtl', context) or should_run_stage('power_rtl', context):
                flags = []
                for p in p_sim_rtl_in:
                    flags += ["-p", p]
                flags += ["--sim_rundir", sim_rtl_run_dir]
                run_hammer_action(f"sim{suffix}", flags)
            else:
                raise AirflowSkipException("sim_rtl task skipped")

        @task
        def sim_to_power(sim_rtl_out, power_sim_rtl_in, **context):
            if should_run_stage('power_rtl', context) or should_run_stage('power_syn', context) or should_run_stage('power_par', context):
                run_hammer_action("sim-to-power", ["-p", sim_rtl_out, -"-o", power_sim_rtl_in])
            else:
                raise AirflowSkipException("sim-to-power skipped")

        @task
        def power_rtl(suffix, power_sim_rtl_in, power_rtl_run_dir, **context):
            if should_run_stage('power_rtl', context):
                run_hammer_action(f"power{suffix}", ["-p", power_sim_rtl_in, "--power_rundir", power_rtl_run_dir])
            else:
                raise AirflowSkipException("power_rtl task skipped")

        @task
        def syn(suffix, p_syn_in, **context):
            if should_run_stage('syn', context):
                flags = []
                for p in p_syn_in:
                    flags += ["-p", p]
                run_hammer_action(f"syn{suffix}", flags)
            else:
                raise AirflowSkipException("syn task skipped")

        @task
        def syn_to_sim(syn_out, sim_syn_in, **context):
            if should_run_stage('sim_syn', context) or should_run_stage('power_syn', context):
                run_hammer_action("syn-to-sim", ["-p", syn_out, "-o", sim_syn_in])
            else:
                raise AirflowSkipException("syn-to-sim skipped")

        @task
        def sim_syn(suffix, sim_syn_in, sim_syn_run_dir, **context):
            if should_run_stage('sim_syn', context) or should_run_stage('power_syn', context):
                run_hammer_action(f"sim{suffix}", ["-p", sim_syn_in, "--sim_rundir", sim_syn_run_dir])
            else:
                raise AirflowSkipException("sim_syn task skipped")

        @task
        def syn_to_power(syn_out, power_syn_in, **context):
            if should_run_stage('power_syn', context):
                run_hammer_action("syn-to-power", ["-p", syn_out, "-o", power_syn_in])
            else:
                raise AirflowSkipException("syn-to-power skipped")

        @task
        def power_syn(suffix, power_sim_syn_in, power_syn_in, power_syn_run_dir, **context):
            if should_run_stage('power_syn', context):
                run_hammer_action(f"power{suffix}", ["-p", power_sim_syn_in, "-p", power_syn_in, "--power_rundir", power_syn_run_dir])
            else:
                raise AirflowSkipException("power_syn task skipped")

        @task
        def syn_to_par(syn_out, par_in, **context):
            if should_run_stage('par', context) or should_run_stage('drc', context) or should_run_stage('lvs', context) or should_run_stage('sim_par', context) or should_run_stage('timing_par', context) or should_run_stage('formal_par', context) or should_run_stage('power_par', context):
                run_hammer_action("syn-to-par", ["-p", syn_out, "-o", par_in])
            else:
                raise AirflowSkipException("syn-to-par skipped")

        @task
        def par(suffix, par_in, **context):
            if should_run_stage('par', context):
                run_hammer_action(f"par{suffix}", ["-p", par_in])
            else:
                raise AirflowSkipException("par task skipped")

        @task
        def par_to_sim(par_out, sim_par_in, **context):
            if should_run_stage('sim_par', context) or should_run_stage('power_par', context):
                run_hammer_action("par-to-sim", ["-p", par_out, "-o", sim_par_in])
            else:
                raise AirflowSkipException("par-to-sim skipped")

        @task
        def sim_par(suffix, sim_par_in, sim_par_run_dir, **context):
            if should_run_stage('sim_par', context) or should_run_stage('power_par', context):
                run_hammer_action(f"sim{suffix}", ["-p", sim_par_in, "--sim_rundir", sim_par_run_dir])
            else:
                raise AirflowSkipException("sim_par task skipped")

        @task
        def par_to_power(par_out, power_par_in, **context):
            if should_run_stage('power_par', context):
                run_hammer_action("par-to-power", ["-p", par_out, "-o", power_par_in])
            else:
                raise AirflowSkipException("par-to-power skipped")

        @task
        def power_par(suffix, power_sim_par_in, power_par_in, power_par_run_dir, **context):
            if should_run_stage('power_par', context):
                run_hammer_action(f"power{suffix}", ["-p", power_sim_par_in, "-p", power_par_in, "--power_rundir", power_par_run_dir])
            else:
                raise AirflowSkipException("power_par task skipped")

        @task
        def par_to_formal(par_out, formal_par_in, **context):
            if should_run_stage('formal_par', context):
                run_hammer_action("par-to-formal", ["-p", par_out, "-o", formal_par_in])
            else:
                raise AirflowSkipException("par-to-formal skipped")

        @task
        def formal_par(suffix, formal_par_in, formal_par_run_dir, **context):
            if should_run_stage('formal_par', context):
                run_hammer_action(f"formal{suffix}", ["-p", formal_par_in, "--formal_rundir", formal_par_run_dir])
            else:
                raise AirflowSkipException("formal_par task skipped")

        @task
        def par_to_timing(par_out, timing_par_in, **context):
            if should_run_stage('timing_par', context):
                run_hammer_action("par-to-timing", ["-p", par_out, "-o", timing_par_in])
            else:
                raise AirflowSkipException("par-to-timing skipped")

        @task
        def timing_par(suffix, timing_par_in, timing_par_run_dir, **context):
            if should_run_stage('timing_par', context):
                run_hammer_action(f"timing{suffix}", ["-p", timing_par_in, "--timing_rundir", timing_par_run_dir])
            else:
                raise AirflowSkipException("timing_par task skipped")

        @task
        def par_to_drc(par_out, drc_in, **context):
            if should_run_stage('drc', context):
                run_hammer_action("par-to-drc", ["-p", par_out, "-o", drc_in])
            else:
                raise AirflowSkipException("par-to-drc skipped")

        @task
        def drc(suffix, drc_in, **context):
            if should_run_stage('drc', context):
                run_hammer_action(f"drc{suffix}", ["-p", drc_in])
            else:
                raise AirflowSkipException("drc task skipped")

        @task
        def par_to_lvs(par_out, lvs_in, **context):
            if should_run_stage('lvs', context):
                run_hammer_action("par-to-lvs", ["-p", par_out, "-o", lvs_in])
            else:
                raise AirflowSkipException("par-to-lvs skipped")

        @task
        def lvs(suffix, lvs_in, **context):
            if should_run_stage('lvs', context):
                run_hammer_action(f"lvs{suffix}", ["-p", lvs_in])
            else:
                raise AirflowSkipException("lvs task skipped")

        @task
        def syn_to_formal(syn_out, formal_syn_in, **context):
            if should_run_stage('formal_syn', context):
                run_hammer_action("syn-to-formal", ["-p", syn_out, "-o", formal_syn_in])
            else:
                raise AirflowSkipException("syn-to-formal skipped")

        @task
        def formal_syn(suffix, formal_syn_in, formal_syn_run_dir, **context):
            if should_run_stage('formal_syn', context):
                run_hammer_action(f"formal{suffix}", ["-p", formal_syn_in, "--formal_rundir", formal_syn_run_dir])
            else:
                raise AirflowSkipException("formal_syn task skipped")

        @task
        def syn_to_timing(syn_out, timing_syn_in, **context):
            if should_run_stage('timing_syn', context):
                run_hammer_action("syn-to-timing", ["-p", syn_out, "-o", timing_syn_in])
            else:
                raise AirflowSkipException("syn-to-timing skipped")

        @task
        def timing_syn(suffix, timing_syn_in, timing_syn_run_dir, **context):
            if should_run_stage('timing_syn', context):
                run_hammer_action(f"timing{suffix}", ["-p", timing_syn_in, "--timing_rundir", timing_syn_run_dir])
            else:
                raise AirflowSkipException("timing_syn task skipped")

        @task(task_id="hier_par_to_syn")
        def hier_par_to_syn(pstring, syn_deps, **context):
            flags = []
            for ps in pstring:
                flags += ["-p", ps]
            flags += ["-o", syn_deps]
            run_hammer_action("hier-par-to-syn", flags)
    """)

    # 3. Parameter Inputs & DAG Skeleton Generation
    output += """
@dag(
    dag_id='hammer_vlsi_flow',
    default_args=default_args,
    schedule=None,
    catchup=False,
    params={
        'sim_rtl': Param(default=False, type='boolean', title='RTL Simulation'),
        'power_rtl': Param(default=False, type='boolean', title='RTL Power Simulation'),
        'syn': Param(default=False, type='boolean', title='Synthesis'),
        'sim_syn': Param(default=False, type='boolean', title='Simulation Synthesis'),
        'timing_syn': Param(default=False, type='boolean', title='Timing Synthesis'),
        'formal_syn': Param(default=False, type='boolean', title='Formal Synthesis'),
        'power_syn': Param(default=False, type='boolean', title='Power Synthesis'),
        'par': Param(default=False, type='boolean', title='Place and Route'),
        'drc': Param(default=False, type='boolean', title='Design Rule Check'),
        'lvs': Param(default=False, type='boolean', title='Layout Versus Schematic'),
        'sim_par': Param(default=False, type='boolean', title='Simulation Place and Route'),
        'timing_par': Param(default=False, type='boolean', title='Timing Place and Route'),
        'formal_par': Param(default=False, type='boolean', title='Formal Place and Route'),
        'power_par': Param(default=False, type='boolean', title='Power Place and Route'),
    },
    render_template_as_native_obj=True
)
def hammer_dag():

    @task(task_id="start")
    def start(**context):
        print("Starting Hammer Flow Pipeline Execution Orchestration...")

    @task(task_id="exit_", trigger_rule=TriggerRule.NONE_FAILED)
    def exit_():
        print("Exiting flow safely.")

    def create_module_pipeline(mod_name, suffix, paths_dict):
        with TaskGroup(group_id=f"module_{mod_name or 'Top'}") as tg:
            
            # 1. RTL Simulation Track
            s_rtl = sim_rtl(suffix, paths_dict['p_sim_rtl_in'], paths_dict['sim_rtl_run_dir'])
            s_rtl_to_p = sim_to_power(paths_dict['sim_rtl_out'], paths_dict['power_sim_rtl_in'])
            p_rtl = power_rtl(suffix, paths_dict['power_sim_rtl_in'], paths_dict['power_rtl_run_dir'])

            # 2. Synthesis Track
            s_node = syn(suffix, paths_dict['p_syn_in'])
            s_to_sim = syn_to_sim(paths_dict['syn_out'], paths_dict['sim_syn_in'])
            s_syn = sim_syn(suffix, paths_dict['sim_syn_in'], paths_dict['sim_syn_run_dir'])
            s_to_p = syn_to_power(paths_dict['syn_out'], paths_dict['power_syn_in'])
            s_syn_to_p = sim_to_power(paths_dict['sim_syn_out'], paths_dict['power_sim_syn_in'])
            p_syn = power_syn(suffix, paths_dict['power_sim_syn_in'], paths_dict['power_syn_in'], paths_dict['power_syn_run_dir'])

            # Post-Synthesis Verification Steps
            s_to_form = syn_to_formal(paths_dict['syn_out'], paths_dict['formal_syn_in'])
            f_syn = formal_syn(suffix, paths_dict['formal_syn_in'], paths_dict['formal_syn_run_dir'])
            s_to_time = syn_to_timing(paths_dict['syn_out'], paths_dict['timing_syn_in'])
            t_syn = timing_syn(suffix, paths_dict['timing_syn_in'], paths_dict['timing_syn_run_dir'])

            # 3. Place & Route Track
            s_to_par = syn_to_par(paths_dict['syn_out'], paths_dict['par_in'])
            p_node = par(suffix, paths_dict['par_in'])

            # Post-P&R Verification Signoffs
            p_to_sim = par_to_sim(paths_dict['par_out'], paths_dict['sim_par_in'])
            s_par = sim_par(suffix, paths_dict['sim_par_in'], paths_dict['sim_par_run_dir'])
            p_to_p = par_to_power(paths_dict['par_out'], paths_dict['power_par_in'])
            s_par_to_p = sim_to_power(paths_dict['sim_par_out'], paths_dict['power_sim_par_in'])
            p_par = power_par(suffix, paths_dict['power_sim_par_in'], paths_dict['power_par_in'], paths_dict['power_par_run_dir'])

            p_to_form = par_to_formal(paths_dict['par_out'], paths_dict['formal_par_in'])
            f_par = formal_par(suffix, paths_dict['formal_par_in'], paths_dict['formal_par_run_dir'])
            p_to_time = par_to_timing(paths_dict['par_out'], paths_dict['timing_par_in'])
            t_par = timing_par(suffix, paths_dict['timing_par_in'], paths_dict['timing_par_run_dir'])

            # Physical Verification
            p_to_drc = par_to_drc(paths_dict['par_out'], paths_dict['drc_in'])
            d_node = drc(suffix, paths_dict['drc_in'])
            p_to_lvs = par_to_lvs(paths_dict['par_out'], paths_dict['lvs_in'])
            l_node = lvs(suffix, paths_dict['lvs_in'])

            # --- Explicit Flow Pipeline Interconnect Routing Topology ---
            s_rtl >> s_rtl_to_p >> p_rtl
            
            s_node >> [s_to_sim, s_to_p, s_to_form, s_to_time, s_to_par]
            s_to_sim >> s_syn >> s_syn_to_p
            [s_to_p, s_syn_to_p] >> p_syn
            s_to_form >> f_syn
            s_to_time >> t_syn
            
            s_to_par >> p_node
            p_node >> [p_to_sim, p_to_p, p_to_form, p_to_time, p_to_drc, p_to_lvs]
            p_to_sim >> s_par >> s_par_to_p
            [p_to_p, s_par_to_p] >> p_par
            p_to_form >> f_par
            p_to_time >> t_par
            p_to_drc >> d_node
            p_to_lvs >> l_node

        return tg

    start_node = start()
    exit_node = exit_()
"""

    # 4. Programmatic Inter-Module Routing Map
    if not dependency_graph:
        top_module = str(driver.database.get_setting("synthesis.inputs.top_module"))
        
        # Build 1:1 matching directory mapping dictionary literal values
        output += f"""
    paths_{top_module} = {{
        'sim_rtl_run_dir': os.path.join(OBJ_DIR, "sim-rtl-rundir"),
        'power_rtl_run_dir': os.path.join(OBJ_DIR, "power-rtl-rundir"),
        'syn_run_dir': os.path.join(OBJ_DIR, "syn-rundir"),
        'sim_syn_run_dir': os.path.join(OBJ_DIR, "sim-syn-rundir"),
        'power_syn_run_dir': os.path.join(OBJ_DIR, "power-syn-rundir"),
        'par_run_dir': os.path.join(OBJ_DIR, "par-rundir"),
        'sim_par_run_dir': os.path.join(OBJ_DIR, "sim-par-rundir"),
        'power_par_run_dir': os.path.join(OBJ_DIR, "power-par-rundir"),
        'drc_run_dir': os.path.join(OBJ_DIR, "drc-rundir"),
        'lvs_run_dir': os.path.join(OBJ_DIR, "lvs-rundir"),
        'formal_syn_run_dir': os.path.join(OBJ_DIR, "formal-syn-rundir"),
        'formal_par_run_dir': os.path.join(OBJ_DIR, "formal-par-rundir"),
        'timing_syn_run_dir': os.path.join(OBJ_DIR, "timing-syn-rundir"),
        'timing_par_run_dir': os.path.join(OBJ_DIR, "timing-par-rundir"),
        'p_sim_rtl_in': PROJ_CONFIGS,
        'sim_rtl_out': os.path.join(os.path.join(OBJ_DIR, "sim-rtl-rundir"), "sim-output-full.json"),
        'power_sim_rtl_in': os.path.join(OBJ_DIR, "power-sim-rtl-input.json"),
        'power_rtl_out': os.path.join(os.path.join(OBJ_DIR, "power-rtl-rundir"), "power-output-full.json"),
        'p_syn_in': PROJ_CONFIGS,
        'syn_out': os.path.join(os.path.join(OBJ_DIR, "syn-rundir"), "syn-output-full.json"),
        'sim_syn_in': os.path.join(OBJ_DIR, "sim-syn-input.json"),
        'sim_syn_out': os.path.join(os.path.join(OBJ_DIR, "sim-syn-rundir"), "sim-output-full.json"),
        'power_sim_syn_in': os.path.join(OBJ_DIR, "power-sim-syn-input.json"),
        'power_syn_in': os.path.join(OBJ_DIR, "power-syn-input.json"),
        'power_syn_out': os.path.join(os.path.join(OBJ_DIR, "power-syn-rundir"), "power-output-full.json"),
        'par_in': os.path.join(OBJ_DIR, "par-input.json"),
        'par_out': os.path.join(os.path.join(OBJ_DIR, "par-rundir"), "par-output-full.json"),
        'sim_par_in': os.path.join(OBJ_DIR, "sim-par-input.json"),
        'sim_par_out': os.path.join(os.path.join(OBJ_DIR, "sim-par-rundir"), "sim-output-full.json"),
        'power_sim_par_in': os.path.join(OBJ_DIR, "power-sim-par-input.json"),
        'power_par_in': os.path.join(OBJ_DIR, "power-par-input.json"),
        'power_par_out': os.path.join(os.path.join(OBJ_DIR, "power-par-rundir"), "power-output-full.json"),
        'drc_in': os.path.join(OBJ_DIR, "drc-input.json"),
        'drc_out': os.path.join(os.path.join(OBJ_DIR, "drc-rundir"), "drc-output-full.json"),
        'lvs_in': os.path.join(OBJ_DIR, "lvs-input.json"),
        'lvs_out': os.path.join(os.path.join(OBJ_DIR, "lvs-rundir"), "lvs-output-full.json"),
        'formal_syn_in': os.path.join(OBJ_DIR, "formal-syn-input.json"),
        'formal_syn_out': os.path.join(os.path.join(OBJ_DIR, "formal-syn-rundir"), "formal-output-full.json"),
        'formal_par_in': os.path.join(OBJ_DIR, "formal-par-input.json"),
        'formal_par_out': os.path.join(os.path.join(OBJ_DIR, "formal-par-rundir"), "formal-output-full.json"),
        'timing_syn_in': os.path.join(OBJ_DIR, "timing-syn-input.json"),
        'timing_syn_out': os.path.join(os.path.join(OBJ_DIR, "timing-syn-rundir"), "timing-output-full.json"),
        'timing_par_in': os.path.join(OBJ_DIR, "timing-par-input.json"),
        'timing_par_out': os.path.join(os.path.join(OBJ_DIR, "timing-par-rundir"), "timing-output-full.json")
    }}
    mod_tg = create_module_pipeline('{top_module}', '', paths_{top_module})
    start_node >> mod_tg >> exit_node
"""
    else:
        output += "    pipelines = {}\n"
        
        # Build out structural path sets for every individual macro block in the graph
        for node, edges in dependency_graph.items():
            out_edges = edges[1]
            
            p_syn_in_expression = "PROJ_CONFIGS"
            if len(out_edges) > 0:
                p_syn_in_expression = f"[os.path.join(OBJ_DIR, 'syn-{node}-input.json')]"

            output += f"""
    paths_{node} = {{
        'sim_rtl_run_dir': os.path.join(OBJ_DIR, "sim-rtl-{node}"),
        'power_rtl_run_dir': os.path.join(OBJ_DIR, "power-rtl-{node}"),
        'syn_run_dir': os.path.join(OBJ_DIR, "syn-{node}"),
        'sim_syn_run_dir': os.path.join(OBJ_DIR, "sim-syn-{node}"),
        'power_syn_run_dir': os.path.join(OBJ_DIR, "power-syn-{node}"),
        'par_run_dir': os.path.join(OBJ_DIR, "par-{node}"),
        'sim_par_run_dir': os.path.join(OBJ_DIR, "sim-par-{node}"),
        'power_par_run_dir': os.path.join(OBJ_DIR, "power-par-{node}"),
        'drc_run_dir': os.path.join(OBJ_DIR, "drc-{node}"),
        'lvs_run_dir': os.path.join(OBJ_DIR, "lvs-{node}"),
        'formal_syn_run_dir': os.path.join(OBJ_DIR, "formal-syn-{node}"),
        'formal_par_run_dir': os.path.join(OBJ_DIR, "formal-par-{node}"),
        'timing_syn_run_dir': os.path.join(OBJ_DIR, "timing-syn-{node}"),
        'timing_par_run_dir': os.path.join(OBJ_DIR, "timing-par-{node}"),
        'p_sim_rtl_in': PROJ_CONFIGS,
        'sim_rtl_out': os.path.join(os.path.join(OBJ_DIR, "sim-rtl-{node}"), "sim-output-full.json"),
        'power_sim_rtl_in': os.path.join(OBJ_DIR, "power-sim-rtl-{node}-input.json"),
        'power_rtl_out': os.path.join(os.path.join(OBJ_DIR, "power-rtl-{node}"), "power-output-full.json"),
        'p_syn_in': {p_syn_in_expression},
        'syn_out': os.path.join(os.path.join(OBJ_DIR, "syn-{node}"), "syn-output-full.json"),
        'sim_syn_in': os.path.join(OBJ_DIR, "sim-syn-{node}-input.json"),
        'sim_syn_out': os.path.join(os.path.join(OBJ_DIR, "sim-syn-{node}"), "sim-output-full.json"),
        'power_sim_syn_in': os.path.join(OBJ_DIR, "power-sim-syn-{node}-input.json"),
        'power_syn_in': os.path.join(OBJ_DIR, "power-syn-{node}-input.json"),
        'power_syn_out': os.path.join(os.path.join(OBJ_DIR, "power-syn-{node}"), "power-output-full.json"),
        'par_in': os.path.join(OBJ_DIR, "par-{node}-input.json"),
        'par_out': os.path.join(os.path.join(OBJ_DIR, "par-{node}"), "par-output-full.json"),
        'sim_par_in': os.path.join(OBJ_DIR, "sim-par-{node}-input.json"),
        'sim_par_out': os.path.join(os.path.join(OBJ_DIR, "sim-par-{node}"), "sim-output-full.json"),
        'power_sim_par_in': os.path.join(OBJ_DIR, "power-sim-par-{node}-input.json"),
        'power_par_in': os.path.join(OBJ_DIR, "power-par-{node}-input.json"),
        'power_par_out': os.path.join(os.path.join(OBJ_DIR, "power-par-{node}"), "power-output-full.json"),
        'drc_in': os.path.join(OBJ_DIR, "drc-{node}-input.json"),
        'drc_out': os.path.join(os.path.join(OBJ_DIR, "drc-{node}"), "drc-output-full.json"),
        'lvs_in': os.path.join(OBJ_DIR, "lvs-{node}-input.json"),
        'lvs_out': os.path.join(os.path.join(OBJ_DIR, "lvs-{node}"), "lvs-output-full.json"),
        'formal_syn_in': os.path.join(OBJ_DIR, "formal-syn-{node}-input.json"),
        'formal_syn_out': os.path.join(os.path.join(OBJ_DIR, "formal-syn-{node}"), "formal-output-full.json"),
        'formal_par_in': os.path.join(OBJ_DIR, "formal-par-{node}-input.json"),
        'formal_par_out': os.path.join(os.path.join(OBJ_DIR, "formal-par-{node}"), "formal-output-full.json"),
        'timing_syn_in': os.path.join(OBJ_DIR, "timing-syn-{node}-input.json"),
        'timing_syn_out': os.path.join(os.path.join(OBJ_DIR, "timing-syn-{node}"), "timing-output-full.json"),
        'timing_par_in': os.path.join(OBJ_DIR, "timing-par-{node}-input.json"),
        'timing_par_out': os.path.join(os.path.join(OBJ_DIR, "timing-par-{node}"), "timing-output-full.json")
    }}
"""

        for node, edges in dependency_graph.items():
            if len(edges[1]) == 0:
                output += "    pipelines['{0}'] = create_module_pipeline('{0}', '-{0}', paths_{0})\n".format(node)
                output += "    start_node >> pipelines['{0}']\n".format(node)
        
        for node, edges in dependency_graph.items():
            out_edges = edges[1]
            if len(out_edges) > 0:
                child_arr = ", ".join([f"pipelines['{x}']" for x in out_edges])
                out_confs_list = ", ".join([f"os.path.join(OBJ_DIR, 'par-{x}', 'par-output-full.json')" for x in out_edges])
                
                output += f"""
    # Hierarchical Assembly for macro: {node}
    pstring_{node} = [{out_confs_list}]
    syn_deps_{node} = os.path.join(OBJ_DIR, "syn-{node}-input.json")
    hier_bridge_{node} = hier_par_to_syn.override(task_id='hier_par_to_syn_{node}')(pstring=pstring_{node}, syn_deps=syn_deps_{node})
    pipelines['{node}'] = create_module_pipeline('{node}', '-{node}', paths_{node})
    [{child_arr}] >> hier_bridge_{node} >> pipelines['{node}']
"""
        
        all_nodes = list(dependency_graph.keys())
        output += "    # Route all workflow paths safely to exit entrypoint\n"
        output += "    exit_drivers = [pipelines[x] for x in {0}]\n".format(all_nodes)
        output += "    exit_drivers >> exit_node\n"

    output += "\ndag_instance = hammer_dag()\n"

    with open(dag_file, "w") as f:
        f.write(output)

    return dependency_graph

BuildSystems = {
    "make": build_makefile,
    "none": build_noop
}