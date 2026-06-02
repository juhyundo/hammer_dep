#  hammer_build_systems.py
#  Class containing all the methods to create VLSI flow build system infrastructure

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
            Spawns a clean python subprocess executing the target stage.
            \"\"\"
            print(f"Running active Hammer Action: {{action}}")
            cmd = [sys.executable, HAMMER_EXEC]
            for env in ENV_CONFIGS:
                cmd += ["-e", env]
            for proj in PROJ_CONFIGS:
                cmd += ["-p", proj]
            if extra_flags:
                cmd += extra_flags
            cmd += ["--obj_dir", OBJ_DIR, action]
            
            print(f"Executing Process Command: {{' '.join(cmd)}}")
            res = subprocess.run(cmd)
            if res.returncode != 0:
                raise AirflowFailException(f"Hammer action {{action}} failed with exit code {{res.returncode}}")
    """)

    # 2. Re-implement tasks matching context['dag_run'].conf configuration validation strategy
    output += textwrap.dedent("""
        @task
        def sim_rtl(suffix, run_dir, **context):
            if context['dag_run'].conf.get('sim_rtl', False):
                run_hammer_action(f"sim{suffix}", ["--sim_rundir", run_dir])
            else:
                raise AirflowSkipException("sim_rtl task skipped")

        @task
        def sim_to_power(**context):
            if context['dag_run'].conf.get('power_rtl', False):
                run_hammer_action("sim-to-power")
            else:
                raise AirflowSkipException("sim-to-power skipped")

        @task
        def power_rtl(suffix, run_dir, **context):
            if context['dag_run'].conf.get('power_rtl', False):
                run_hammer_action(f"power{suffix}", ["--power_rundir", run_dir])
            else:
                raise AirflowSkipException("power_rtl task skipped")

        @task
        def syn(suffix, **context):
            if context['dag_run'].conf.get('syn', False):
                run_hammer_action(f"syn{suffix}")
            else:
                raise AirflowSkipException("syn task skipped")

        @task
        def syn_to_sim(**context):
            if context['dag_run'].conf.get('sim_syn', False):
                run_hammer_action("syn-to-sim")
            else:
                raise AirflowSkipException("syn-to-sim skipped")

        @task
        def sim_syn(suffix, run_dir, **context):
            if context['dag_run'].conf.get('sim_syn', False):
                run_hammer_action(f"sim{suffix}", ["--sim_rundir", run_dir])
            else:
                raise AirflowSkipException("sim_syn task skipped")

        @task
        def syn_to_power(**context):
            if context['dag_run'].conf.get('power_syn', False):
                run_hammer_action("syn-to-power")
            else:
                raise AirflowSkipException("syn-to-power skipped")

        @task
        def sim_syn_to_power(**context):
            if context['dag_run'].conf.get('power_syn', False):
                run_hammer_action("sim-to-power")
            else:
                raise AirflowSkipException("sim_syn_to_power skipped")

        @task
        def power_syn(suffix, run_dir, **context):
            if context['dag_run'].conf.get('power_syn', False):
                run_hammer_action(f"power{suffix}", ["--power_rundir", run_dir])
            else:
                raise AirflowSkipException("power_syn task skipped")

        @task
        def syn_to_formal(**context):
            if context['dag_run'].conf.get('formal_syn', False):
                run_hammer_action("syn-to-formal")
            else:
                raise AirflowSkipException("syn-to-formal skipped")

        @task
        def formal_syn(suffix, **context):
            if context['dag_run'].conf.get('formal_syn', False):
                run_hammer_action(f"formal{suffix}")
            else:
                raise AirflowSkipException("formal_syn task skipped")

        @task
        def syn_to_timing(**context):
            if context['dag_run'].conf.get('timing_syn', False):
                run_hammer_action("syn-to-timing")
            else:
                raise AirflowSkipException("syn-to-timing skipped")

        @task
        def timing_syn(suffix, **context):
            if context['dag_run'].conf.get('timing_syn', False):
                run_hammer_action(f"timing{suffix}")
            else:
                raise AirflowSkipException("timing_syn task skipped")

        @task
        def syn_to_par(**context):
            if context['dag_run'].conf.get('par', False):
                run_hammer_action("syn-to-par")
            else:
                raise AirflowSkipException("syn-to-par skipped")

        @task
        def par(suffix, **context):
            if context['dag_run'].conf.get('par', False):
                run_hammer_action(f"par{suffix}")
            else:
                raise AirflowSkipException("par task skipped")

        @task
        def par_to_sim(**context):
            if context['dag_run'].conf.get('sim_par', False):
                run_hammer_action("par-to-sim")
            else:
                raise AirflowSkipException("par-to-sim skipped")

        @task
        def sim_par(suffix, run_dir, **context):
            if context['dag_run'].conf.get('sim_par', False):
                run_hammer_action(f"sim{suffix}", ["--sim_rundir", run_dir])
            else:
                raise AirflowSkipException("sim_par task skipped")

        @task
        def par_to_power(**context):
            if context['dag_run'].conf.get('power_par', False):
                run_hammer_action("par-to-power")
            else:
                raise AirflowSkipException("par-to-power skipped")

        @task
        def sim_par_to_power(**context):
            if context['dag_run'].conf.get('power_par', False):
                run_hammer_action("sim-to-power")
            else:
                raise AirflowSkipException("sim_par_to_power skipped")

        @task
        def power_par(suffix, run_dir, **context):
            if context['dag_run'].conf.get('power_par', False):
                run_hammer_action(f"power{suffix}", ["--power_rundir", run_dir])
            else:
                raise AirflowSkipException("power_par task skipped")

        @task
        def par_to_formal(**context):
            if context['dag_run'].conf.get('formal_par', False):
                run_hammer_action("par-to-formal")
            else:
                raise AirflowSkipException("par-to-formal skipped")

        @task
        def formal_par(suffix, **context):
            if context['dag_run'].conf.get('formal_par', False):
                run_hammer_action(f"formal{suffix}")
            else:
                raise AirflowSkipException("formal_par task skipped")

        @task
        def par_to_timing(**context):
            if context['dag_run'].conf.get('timing_par', False):
                run_hammer_action("par-to-timing")
            else:
                raise AirflowSkipException("par-to-timing skipped")

        @task
        def timing_par(suffix, **context):
            if context['dag_run'].conf.get('timing_par', False):
                run_hammer_action(f"timing{suffix}")
            else:
                raise AirflowSkipException("timing_par task skipped")

        @task
        def par_to_drc(**context):
            if context['dag_run'].conf.get('drc', False):
                run_hammer_action("par-to-drc")
            else:
                raise AirflowSkipException("par-to-drc skipped")

        @task
        def drc(suffix, **context):
            if context['dag_run'].conf.get('drc', False):
                run_hammer_action(f"drc{suffix}")
            else:
                raise AirflowSkipException("drc task skipped")

        @task
        def par_to_lvs(**context):
            if context['dag_run'].conf.get('lvs', False):
                run_hammer_action("par-to-lvs")
            else:
                raise AirflowSkipException("par-to-lvs skipped")

        @task
        def lvs(suffix, **context):
            if context['dag_run'].conf.get('lvs', False):
                run_hammer_action(f"lvs{suffix}")
            else:
                raise AirflowSkipException("lvs task skipped")

        @task(task_id="hier_par_to_syn")
        def hier_par_to_syn(**context):
            run_hammer_action("hier-par-to-syn")
    """)

    # 3. Base Parameter Input Configuration mapping hammer_dag.py
    output += textwrap.dedent("""
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

            def create_module_pipeline(mod_name, suffix):
                with TaskGroup(group_id=f"module_{mod_name or 'Top'}") as tg:
                    sim_rtl_run = os.path.join(OBJ_DIR, f"sim-rtl-{mod_name}")
                    power_rtl_run = os.path.join(OBJ_DIR, f"power-rtl-{mod_name}")
                    sim_syn_run = os.path.join(OBJ_DIR, f"sim-syn-{mod_name}")
                    power_syn_run = os.path.join(OBJ_DIR, f"power-syn-{mod_name}")
                    sim_par_run = os.path.join(OBJ_DIR, f"sim-par-{mod_name}")
                    power_par_run = os.path.join(OBJ_DIR, f"power-par-{mod_name}")

                    # 1. RTL Simulation Track
                    s_rtl = sim_rtl(suffix, sim_rtl_run)
                    s_rtl_to_p = sim_to_power()
                    p_rtl = power_rtl(suffix, power_rtl_run)

                    # 2. Synthesis Track
                    s_node = syn(suffix)
                    s_to_sim = syn_to_sim()
                    s_syn = sim_syn(suffix, sim_syn_run)
                    s_to_p = syn_to_power()
                    s_syn_to_p = sim_syn_to_power()
                    p_syn = power_syn(suffix, power_syn_run)

                    # Post-Synthesis Sign-Off Verification Steps
                    s_to_form = syn_to_formal()
                    f_syn = formal_syn(suffix)
                    s_to_time = syn_to_timing()
                    t_syn = timing_syn(suffix)

                    # 3. Place & Route Track
                    s_to_par = syn_to_par()
                    p_node = par(suffix)

                    # Post-P&R Signoff Verification
                    p_to_sim = par_to_sim()
                    s_par = sim_par(suffix, sim_par_run)
                    p_to_p = par_to_power()
                    s_par_to_p = sim_par_to_power()
                    p_par = power_par(suffix, power_par_run)

                    # Post-P&R Parallel Signoff Extensions
                    p_to_form = par_to_formal()
                    f_par = formal_par(suffix)
                    p_to_time = par_to_timing()
                    t_par = timing_par(suffix)

                    # Physical Verification
                    p_to_drc = par_to_drc()
                    d_node = drc(suffix)
                    p_to_lvs = par_to_lvs()
                    l_node = lvs(suffix)

                    # --- Structural Dependencies inside the module group ---
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
    """)

    # 4. Programmatic Inter-Module Routing
    if not dependency_graph:
        top_module = str(driver.database.get_setting("synthesis.inputs.top_module"))
        output += "            mod_tg = create_module_pipeline('{0}', '')\n".format(top_module)
        output += "            start_node >> mod_tg >> exit_node\n"
    else:
        output += "            pipelines = {}\n"
        for node, edges in dependency_graph.items():
            if len(edges[1]) == 0:
                output += "            pipelines['{0}'] = create_module_pipeline('{0}', '-{0}')\n".format(node)
                output += "            start_node >> pipelines['{0}']\n".format(node)
        
        for node, edges in dependency_graph.items():
            out_edges = edges[1]
            if len(out_edges) > 0:
                child_arr = ", ".join([f"pipelines['{x}']" for x in out_edges])
                output += "            # Hierarchical Assembly for macro: {0}\n".format(node)
                output += "            hier_bridge_{0} = hier_par_to_syn.override(task_id='hier_par_to_syn_{0}')()\n".format(node)
                output += "            pipelines['{0}'] = create_module_pipeline('{0}', '-{0}')\n".format(node)
                output += "            [{0}] >> hier_bridge_{1} >> pipelines['{1}']\n".format(child_arr, node)
        
        all_nodes = list(dependency_graph.keys())
        output += "            # Route all workflow paths safely to exit entrypoint\n"
        output += "            exit_drivers = [pipelines[x] for x in {0}]\n".format(all_nodes)
        output += "            exit_drivers >> exit_node\n"

    output += "\ndag_instance = hammer_dag()\n"

    with open(dag_file, "w") as f:
        f.write(output)

    return dependency_graph

BuildSystems = {
    "make": build_makefile,
    "none": build_noop
}