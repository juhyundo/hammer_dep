#  hammer_build_systems.py
#  Class containing all the methods to create VLSI flow build system infrastructure
#
#  See LICENSE for licence details.

from .driver import HammerDriver

import os
import sys
import textwrap
from typing import List, Dict, Tuple, Callable, Optional

#import pdb
#pdb.set_trace()

def build_noop(driver: HammerDriver, append_error_func: Callable[[str], None]) -> dict:
    """
    Do nothing, just return the dependency graph.

    :param driver: The HammerDriver
    :return: The dependency graph
    """
    dependency_graph = driver.get_hierarchical_dependency_graph()
    return dependency_graph


def build_makefile(driver: HammerDriver, append_error_func: Callable[[str], None]) -> dict:
    #pdb.set_trace()    #[Trace Path 1]
    """
    Build a Makefile include in the obj_dir called hammer.d. This is intended to be dynamically
    created and included into a top-level Makefile.

    The Makefile will contain targets for the following hammer actions, as well as any necessary
    bridge actions (xyz-to-abc):
        - pcb
        - syn
        - par
        - drc
        - lvs
        - sim-rtl
        - sim-syn
        - sim-par
        - power-rtl
        - power-syn
        - power-par
        - formal-syn
        - formal-par
        - timing-syn
        - timing-par

    For hierarchical flows, the syn, par, drc, lvs, sim, power, formal, and timing actions will all be suffixed with the name
    of the hierarchical modules (e.g. syn-Top, syn-SubModA, par-SubModA, etc.). The appropriate
    dependencies and bridge actions are automatically generated from the hierarchy provided in the
    Hammer IR.

    For actions that can be run at multiple points in the flow such as sim, the name of the target
    will include the action it is being run after (e.g. sim-syn, sim-par, etc.). With no suffix
    an rtl level simulation will be run.

    Additionally, "redo" steps are created (e.g. redo-par for flat designs or redo-par-Top for
    hierarchical), which allow the user to bypass the normal Makefile dependencies and force a
    rerun of a particular task. This is useful when the user wants to change an input Hammer IR file
    knowing it will not affect intermediate steps in the design.

    An example use case for integrating this file into a top flow is provided below. Be sure to use
    real tabs if copying this snippet!

    ```
    TOP ?= MyTop
    OBJ_DIR ?= $(abspath build-$(TOP))
    INPUT_CONFS ?= foo.yaml bar.yaml baz.yaml

    HAMMER_EXEC ?= ./mychip-vlsi.py

    .PHONY: all
    all: drc-$(TOP) lvs-$(TOP)

    GENERATED_CONF = $(OBJ_DIR)/input.yaml

    $(GENERATED_CONF):
        echo "synthesis.inputs.top_module: $(TOP)" > $@
        echo "pcb.inputs.top_module: $(TOP)" >> $@

    $(OBJ_DIR)/hammer.d: $(GENERATED_CONF)
        $(HAMMER_EXEC) -e env.yaml $(foreach x,$(INPUT_CONFS) $(GENERATED_CONF), -p $(x)) --obj_dir $(OBJ_DIR) build

    include $(OBJ_DIR)/hammer.d
    ```

    The generated Makefile has a few variables that are set if absent. This allows the user to override them without
    modifying hammer.d. They are listed as follows:
        - HAMMER_EXEC: This sets the actual python executable containing the HammerDriver main() function. It is set to
          the executable used to generate the Makefile by default.
        - HAMMER_DEPENDENCIES: The list of dependences to use for the initial syn and pcb targets. It is set to the set
          of all input configurations, environment settings, and input files by default.
        - HAMMER_*_DEPENDENCIES: There is a version of this variable for each action. It allows the user to have more
          fine grained depencies compared to the blunt HAMMER_DEPENDENCIES. It is set as a dependency for the respective
          action. Often used with clearing the global HAMMER_DEPENDENCIES.
        - HAMMER_EXTRA_ARGS: This is passed to the Hammer executable for all targets. This is unset by default.
          Its primary uses are for adding additional configuration files with -p, --to_step/until_step, and/or --from_step/
          after_step options. An example use is "make redo-par-Top HAMMER_EXTRA_ARGS="-p patch.yaml --from_step placement".

    :param driver: The HammerDriver
    :return: The dependency graph
    """
    dependency_graph = driver.get_hierarchical_dependency_graph()
    #pdb.set_trace()
    makefile = os.path.join(driver.obj_dir, "hammer.d")
    default_dependencies = driver.options.project_configs + driver.options.environment_configs
    default_dependencies.extend(list(driver.database.get_setting("synthesis.inputs.input_files", [])))
    # Resolve the canonical path for each dependency
    default_dependencies = [os.path.realpath(x) for x in default_dependencies]
    output = "HAMMER_EXEC ?= {}\n".format(os.path.realpath(sys.argv[0]))
    output += "HAMMER_DEPENDENCIES ?= {}\n\n".format(" ".join(default_dependencies))
    syn_deps = "$(HAMMER_DEPENDENCIES)"
    # Get the confs passed into this execution
    env_confs = " ".join(["-e " + os.path.realpath(x) for x in driver.options.environment_configs])
    proj_confs = " ".join(["-p " + os.path.realpath(x) for x in driver.options.project_configs])
    obj_dir = os.path.realpath(driver.obj_dir)

    # Global steps that are the same for hier or flat
    pcb_run_dir = os.path.join(obj_dir, "pcb-rundir")
    pcb_out = os.path.join(pcb_run_dir, "pcb-output-full.json")
    output += textwrap.dedent("""
        ####################################################################################
        ## Global steps temp
        ####################################################################################
        .PHONY: pcb
        pcb: {pcb_out}

        {pcb_out}: {syn_deps}
        \t$(HAMMER_EXEC) {env_confs} {all_inputs} --obj_dir {obj_dir} pcb

        """.format(pcb_out=pcb_out, syn_deps=syn_deps, env_confs=env_confs, all_inputs=proj_confs, obj_dir=obj_dir))

    make_text = textwrap.dedent("""
        ####################################################################################
        ## Steps for {mod}
        ####################################################################################
        .PHONY: sim-rtl{suffix} syn{suffix} syn-to-sim{suffix} sim-syn{suffix} syn-to-par{suffix} par{suffix} par-to-sim{suffix} sim-par{suffix} sim-par-to-power{suffix} par-to-power{suffix} power-par{suffix} power-rtl{suffix} sim-rtl-to-power{suffix} sim-syn-to-power{suffix} syn-to-power{suffix} power-syn{suffix} par-to-drc{suffix} drc{suffix} par-to-lvs{suffix} lvs{suffix} syn-to-formal{suffix} formal-syn{suffix} par-to-formal{suffix} formal-par{suffix} syn-to-timing{suffix} timing-syn{suffix} par-to-timing{suffix} timing-par{suffix}

        sim-rtl{suffix}          : {sim_rtl_out}
        syn{suffix}              : {syn_out}

        syn-to-sim{suffix}       : {sim_syn_in}
        sim-syn{suffix}          : {sim_syn_out}

        syn-to-par{suffix}       : {par_in}
        par{suffix}              : {par_out}

        par-to-sim{suffix}       : {sim_par_in}
        sim-par{suffix}          : {sim_par_out}

        sim-par-to-power{suffix} : {power_sim_par_in}
        par-to-power{suffix}     : {power_par_in}
        power-par{suffix}        : {power_par_out}

        sim-rtl-to-power{suffix} : {power_sim_rtl_in}
        power-rtl{suffix}        : {power_rtl_out}

        sim-syn-to-power{suffix} : {power_sim_syn_in}
        syn-to-power{suffix}     : {power_syn_in}
        power-syn{suffix}        : {power_syn_out}

        par-to-drc{suffix}       : {drc_in}
        drc{suffix}              : {drc_out}

        par-to-lvs{suffix}       : {lvs_in}
        lvs{suffix}              : {lvs_out}

        syn-to-formal{suffix}    : {formal_syn_in}
        formal-syn{suffix}       : {formal_syn_out}

        par-to-formal{suffix}    : {formal_par_in}
        formal-par{suffix}       : {formal_par_out}

        syn-to-timing{suffix}    : {timing_syn_in}
        timing-syn{suffix}       : {timing_syn_out}

        par-to-timing{suffix}    : {timing_par_in}
        timing-par{suffix}       : {timing_par_out}

        {par_to_syn}

        {sim_rtl_out}: {syn_deps} $(HAMMER_SIM_RTL_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} {p_sim_rtl_in} $(HAMMER_EXTRA_ARGS) --sim_rundir {sim_rtl_run_dir} --obj_dir {obj_dir} sim{suffix}

        {power_sim_rtl_in}: {sim_rtl_out}
        \t$(HAMMER_EXEC) {env_confs} -p {sim_rtl_out} $(HAMMER_EXTRA_ARGS) -o {power_sim_rtl_in} --obj_dir {obj_dir} sim-to-power

        {power_rtl_out}: {power_sim_rtl_in} $(HAMMER_POWER_RTL_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {power_sim_rtl_in} $(HAMMER_EXTRA_ARGS) --power_rundir {power_rtl_run_dir} --obj_dir {obj_dir} power{suffix}

        {syn_out}: {syn_deps} $(HAMMER_SYN_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} {p_syn_in} $(HAMMER_EXTRA_ARGS) --obj_dir {obj_dir} syn{suffix}

        {sim_syn_in}: {syn_out}
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {sim_syn_in} --obj_dir {obj_dir} syn-to-sim

        {sim_syn_out}: {sim_syn_in} $(HAMMER_SIM_SYN_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {sim_syn_in} $(HAMMER_EXTRA_ARGS) --sim_rundir {sim_syn_run_dir} --obj_dir {obj_dir} sim{suffix}

        {power_sim_syn_in}: {sim_syn_out}
        \t$(HAMMER_EXEC) {env_confs} -p {sim_syn_out} $(HAMMER_EXTRA_ARGS) -o {power_sim_syn_in} --obj_dir {obj_dir} sim-to-power

        {power_syn_in}: {syn_out}
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {power_syn_in} --obj_dir {obj_dir} syn-to-power

        {power_syn_out}: {power_sim_syn_in} {power_syn_in} $(HAMMER_POWER_SYN_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {power_sim_syn_in} -p {power_syn_in} $(HAMMER_EXTRA_ARGS) --power_rundir {power_syn_run_dir} --obj_dir {obj_dir} power{suffix}

        {par_in}: {syn_out}
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {par_in} --obj_dir {obj_dir} syn-to-par

        {par_out}: {par_in} $(HAMMER_PAR_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {par_in} $(HAMMER_EXTRA_ARGS) --obj_dir {obj_dir} par{suffix}

        {sim_par_in}: {par_out}
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {sim_par_in} --obj_dir {obj_dir} par-to-sim

        {sim_par_out}: {sim_par_in} $(HAMMER_SIM_PAR_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {sim_par_in} $(HAMMER_EXTRA_ARGS) --sim_rundir {sim_par_run_dir} --obj_dir {obj_dir} sim{suffix}

        {power_sim_par_in}: {sim_par_out}
        \t$(HAMMER_EXEC) {env_confs} -p {sim_par_out} $(HAMMER_EXTRA_ARGS) -o {power_sim_par_in} --obj_dir {obj_dir} sim-to-power

        {power_par_in}: {par_out}
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {power_par_in} --obj_dir {obj_dir} par-to-power

        {power_par_out}: {power_sim_par_in} {power_par_in} $(HAMMER_POWER_PAR_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {power_sim_par_in} -p {power_par_in} $(HAMMER_EXTRA_ARGS) --power_rundir {power_par_run_dir} --obj_dir {obj_dir} power{suffix}

        {drc_in}: {par_out}
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {drc_in} --obj_dir {obj_dir} par-to-drc

        {drc_out}: {drc_in} $(HAMMER_DRC_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {drc_in} $(HAMMER_EXTRA_ARGS) --obj_dir {obj_dir} drc{suffix}

        {lvs_in}: {par_out}
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {lvs_in} --obj_dir {obj_dir} par-to-lvs

        {lvs_out}: {lvs_in} $(HAMMER_LVS_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {lvs_in} $(HAMMER_EXTRA_ARGS) --obj_dir {obj_dir} lvs{suffix}

        {formal_syn_in}: {syn_out}
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {formal_syn_in} --obj_dir {obj_dir} syn-to-formal

        {formal_syn_out}: {formal_syn_in} $(HAMMER_FORMAL_SYN_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {formal_syn_in} $(HAMMER_EXTRA_ARGS) --formal_rundir {formal_syn_run_dir} --obj_dir {obj_dir} formal{suffix}

        {formal_par_in}: {par_out}
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {formal_par_in} --obj_dir {obj_dir} par-to-formal

        {formal_par_out}: {formal_syn_in} $(HAMMER_FORMAL_PAR_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {formal_par_in} $(HAMMER_EXTRA_ARGS) --formal_rundir {formal_par_run_dir} --obj_dir {obj_dir} formal{suffix}

        {timing_syn_in}: {syn_out}
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {timing_syn_in} --obj_dir {obj_dir} syn-to-timing

        {timing_syn_out}: {timing_syn_in} $(HAMMER_TIMING_SYN_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {timing_syn_in} $(HAMMER_EXTRA_ARGS) --timing_rundir {timing_syn_run_dir} --obj_dir {obj_dir} timing{suffix}

        {timing_par_in}: {par_out}
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {timing_par_in} --obj_dir {obj_dir} par-to-timing

        {timing_par_out}: {timing_syn_in} $(HAMMER_TIMING_PAR_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {timing_par_in} $(HAMMER_EXTRA_ARGS) --timing_rundir {timing_par_run_dir} --obj_dir {obj_dir} timing{suffix}

        # Redo steps
        # These intentionally break the dependency graph, but allow the flexibility to rerun a step after changing a config.
        # Hammer doesn't know what settings impact synthesis only, e.g., so these are for power-users who "know better."
        # The HAMMER_EXTRA_ARGS variable allows patching in of new configurations with -p or using --to_step or --from_step, for example.
        .PHONY: redo-sim-rtl{suffix} redo-sim-rtl-to-power{suffix} redo-syn{suffix} redo-syn-to-sim{suffix} redo-syn-to-power{suffix} redo-sim-syn{suffix} redo-sim-syn-to-power{suffix} redo-syn-to-par{suffix} redo-par{suffix} redo-par-to-sim{suffix} redo-sim-par{suffix} redo-sim-par-to-power{suffix} redo-par-to-power{suffix} redo-power-par{suffix} redo-par-to-drc{suffix} redo-drc{suffix} redo-par-to-lvs{suffix} redo-lvs{suffix} redo-syn-to-formal{suffix} redo-formal-syn{suffix} redo-par-to-formal{suffix} redo-formal-par{suffix} redo-syn-to-timing{suffix} redo-timing-syn{suffix} redo-par-to-timing{suffix} redo-timing-par{suffix}

        redo-sim-rtl{suffix}:
        \t$(HAMMER_EXEC) {env_confs} {p_sim_rtl_in} $(HAMMER_EXTRA_ARGS) --sim_rundir {sim_rtl_run_dir} --obj_dir {obj_dir} sim{suffix}

        redo-sim-rtl-to-power{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {sim_rtl_out} $(HAMMER_EXTRA_ARGS) -o {power_sim_rtl_in} --obj_dir {obj_dir} sim-to-power

        redo-power-rtl{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {power_sim_rtl_in} $(HAMMER_EXTRA_ARGS) --power_rundir {power_rtl_run_dir} --obj_dir {obj_dir} power{suffix}

        redo-syn{suffix}:
        \t$(HAMMER_EXEC) {env_confs} {p_syn_in} $(HAMMER_EXTRA_ARGS) --obj_dir {obj_dir} syn{suffix}

        redo-syn-to-sim{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {sim_syn_in} --obj_dir {obj_dir} syn-to-sim

        redo-syn-to-power{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {power_syn_in} --obj_dir {obj_dir} syn-to-power

        redo-sim-syn{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {sim_syn_in} $(HAMMER_EXTRA_ARGS) --sim_rundir {sim_syn_run_dir} --obj_dir {obj_dir} sim{suffix}

        redo-sim-syn-to-power{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {sim_syn_out} $(HAMMER_EXTRA_ARGS) -o {power_sim_syn_in} --obj_dir {obj_dir} sim-to-power

        redo-syn-to-par{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {par_in} --obj_dir {obj_dir} syn-to-par

        redo-power-syn{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {power_sim_syn_in} -p {power_syn_in} $(HAMMER_EXTRA_ARGS) --power_rundir {power_syn_run_dir} --obj_dir {obj_dir} power{suffix}

        redo-par{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {par_in} $(HAMMER_EXTRA_ARGS) --obj_dir {obj_dir} par{suffix}

        redo-par-to-sim{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {sim_par_in} --obj_dir {obj_dir} par-to-sim

        redo-sim-par{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {sim_par_in} $(HAMMER_EXTRA_ARGS) --sim_rundir {sim_par_run_dir} --obj_dir {obj_dir} sim{suffix}

        redo-sim-par-to-power{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {sim_par_out} $(HAMMER_EXTRA_ARGS) -o {power_sim_par_in} --obj_dir {obj_dir} sim-to-power

        redo-par-to-power{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {power_par_in} --obj_dir {obj_dir} par-to-power

        redo-power-par{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {power_sim_par_in} -p {power_par_in} $(HAMMER_EXTRA_ARGS) --power_rundir {power_par_run_dir} --obj_dir {obj_dir} power{suffix}

        redo-par-to-drc{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {drc_in} --obj_dir {obj_dir} par-to-drc

        redo-drc{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {drc_in} $(HAMMER_EXTRA_ARGS) --obj_dir {obj_dir} drc{suffix}

        redo-par-to-lvs{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {lvs_in} --obj_dir {obj_dir} par-to-lvs

        redo-lvs{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {lvs_in} $(HAMMER_EXTRA_ARGS) --obj_dir {obj_dir} lvs{suffix}

        redo-syn-to-formal{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {formal_syn_in} --obj_dir {obj_dir} syn-to-formal

        redo-formal-syn{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {formal_syn_in} $(HAMMER_EXTRA_ARGS) --formal_rundir {formal_syn_run_dir} --obj_dir {obj_dir} formal{suffix}

        redo-par-to-formal{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {formal_par_in} --obj_dir {obj_dir} par-to-formal

        redo-formal-par{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {formal_par_in} $(HAMMER_EXTRA_ARGS) --formal_rundir {formal_par_run_dir} --obj_dir {obj_dir} formal{suffix}

        redo-syn-to-timing{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {timing_syn_in} --obj_dir {obj_dir} syn-to-timing

        redo-timing-syn{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {timing_syn_in} $(HAMMER_EXTRA_ARGS) --timing_rundir {timing_syn_run_dir} --obj_dir {obj_dir} timing{suffix}

        redo-par-to-timing{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {timing_par_in} --obj_dir {obj_dir} par-to-timing

        redo-timing-par{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {timing_par_in} $(HAMMER_EXTRA_ARGS) --timing_rundir {timing_par_run_dir} --obj_dir {obj_dir} timing{suffix}

        """)

    if not dependency_graph:
        # Flat flow
        top_module = str(driver.database.get_setting("synthesis.inputs.top_module"))

        # TODO make this DRY
        sim_rtl_run_dir = os.path.join(obj_dir, "sim-rtl-rundir")
        power_rtl_run_dir = os.path.join(obj_dir, "power-rtl-rundir")
        syn_run_dir = os.path.join(obj_dir, "syn-rundir")
        sim_syn_run_dir = os.path.join(obj_dir, "sim-syn-rundir")
        power_syn_run_dir = os.path.join(obj_dir, "power-syn-rundir")
        par_run_dir = os.path.join(obj_dir, "par-rundir")
        sim_par_run_dir = os.path.join(obj_dir, "sim-par-rundir")
        power_par_run_dir = os.path.join(obj_dir, "power-par-rundir")
        drc_run_dir = os.path.join(obj_dir, "drc-rundir")
        lvs_run_dir = os.path.join(obj_dir, "lvs-rundir")
        formal_syn_run_dir = os.path.join(obj_dir, "formal-syn-rundir")
        formal_par_run_dir = os.path.join(obj_dir, "formal-par-rundir")
        timing_syn_run_dir = os.path.join(obj_dir, "timing-syn-rundir")
        timing_par_run_dir = os.path.join(obj_dir, "timing-par-rundir")

        p_sim_rtl_in = proj_confs
        sim_rtl_out = os.path.join(sim_rtl_run_dir, "sim-output-full.json")
        power_sim_rtl_in = os.path.join(obj_dir, "power-sim-rtl-input.json")
        #power_rtl_in = os.path.join(obj_dir, "power-rtl-input.json")
        power_rtl_out = os.path.join(power_rtl_run_dir, "power-output-full.json")
        p_syn_in = proj_confs
        syn_out = os.path.join(syn_run_dir, "syn-output-full.json")
        sim_syn_in = os.path.join(obj_dir, "sim-syn-input.json")
        sim_syn_out = os.path.join(sim_syn_run_dir, "sim-output-full.json")
        power_sim_syn_in = os.path.join(obj_dir, "power-sim-syn-input.json")
        power_syn_in = os.path.join(obj_dir, "power-syn-input.json")
        power_syn_out = os.path.join(power_syn_run_dir, "power-output-full.json")
        par_in = os.path.join(obj_dir, "par-input.json")
        par_out = os.path.join(par_run_dir, "par-output-full.json")
        sim_par_in = os.path.join(obj_dir, "sim-par-input.json")
        sim_par_out = os.path.join(sim_par_run_dir, "sim-output-full.json")
        power_sim_par_in = os.path.join(obj_dir, "power-sim-par-input.json")
        power_par_in = os.path.join(obj_dir, "power-par-input.json")
        power_par_out = os.path.join(power_par_run_dir, "power-output-full.json")
        drc_in = os.path.join(obj_dir, "drc-input.json")
        drc_out = os.path.join(drc_run_dir, "drc-output-full.json")
        lvs_in = os.path.join(obj_dir, "lvs-input.json")
        lvs_out = os.path.join(lvs_run_dir, "lvs-output-full.json")
        formal_syn_in = os.path.join(obj_dir, "formal-syn-input.json")
        formal_syn_out = os.path.join(formal_syn_run_dir, "formal-output-full.json")
        formal_par_in = os.path.join(obj_dir, "formal-par-input.json")
        formal_par_out = os.path.join(formal_par_run_dir, "formal-output-full.json")
        timing_syn_in = os.path.join(obj_dir, "timing-syn-input.json")
        timing_syn_out = os.path.join(timing_syn_run_dir, "timing-output-full.json")
        timing_par_in = os.path.join(obj_dir, "timing-par-input.json")
        timing_par_out = os.path.join(timing_par_run_dir, "timing-output-full.json")

        par_to_syn = ""

        output += make_text.format(suffix="", mod=top_module, env_confs=env_confs, obj_dir=obj_dir, syn_deps=syn_deps,
            par_to_syn=par_to_syn,
            p_sim_rtl_in=p_sim_rtl_in, sim_rtl_out=sim_rtl_out, sim_rtl_run_dir=sim_rtl_run_dir,
            sim_syn_in=sim_syn_in, sim_syn_out=sim_syn_out, sim_syn_run_dir=sim_syn_run_dir,
            power_sim_rtl_in=power_sim_rtl_in, power_rtl_out=power_rtl_out, power_rtl_run_dir=power_rtl_run_dir,
            sim_par_in=sim_par_in, sim_par_out=sim_par_out, sim_par_run_dir=sim_par_run_dir,
            p_syn_in=p_syn_in, syn_out=syn_out, par_in=par_in, par_out=par_out,
            power_sim_syn_in=power_sim_syn_in, power_syn_in=power_syn_in, power_syn_out=power_syn_out, power_syn_run_dir=power_syn_run_dir,
            power_sim_par_in=power_sim_par_in, power_par_in=power_par_in, power_par_out=power_par_out, power_par_run_dir=power_par_run_dir,
            drc_in=drc_in, drc_out=drc_out, lvs_in=lvs_in, lvs_out=lvs_out,
            formal_syn_in=formal_syn_in, formal_syn_out=formal_syn_out, formal_syn_run_dir=formal_syn_run_dir,
            formal_par_in=formal_par_in, formal_par_out=formal_par_out, formal_par_run_dir=formal_par_run_dir,
            timing_syn_in=timing_syn_in, timing_syn_out=timing_syn_out, timing_syn_run_dir=timing_syn_run_dir,
            timing_par_in=timing_par_in, timing_par_out=timing_par_out, timing_par_run_dir=timing_par_run_dir)
    else:
        # Hierarchical flow
        for node, edges in dependency_graph.items():
            out_edges = edges[1]

            # TODO make this DRY
            sim_rtl_run_dir = os.path.join(obj_dir, "sim-rtl-" + node)
            power_rtl_run_dir = os.path.join(obj_dir, "power-rtl-" + node)
            syn_run_dir = os.path.join(obj_dir, "syn-" + node)
            sim_syn_run_dir = os.path.join(obj_dir, "sim-syn-" + node)
            power_syn_run_dir = os.path.join(obj_dir, "power-syn-" + node)
            par_run_dir = os.path.join(obj_dir, "par-" + node)
            sim_par_run_dir = os.path.join(obj_dir, "sim-par-" + node)
            power_par_run_dir = os.path.join(obj_dir, "power-par-" + node)
            drc_run_dir = os.path.join(obj_dir, "drc-" + node)
            lvs_run_dir = os.path.join(obj_dir, "lvs-" + node)
            formal_syn_run_dir = os.path.join(obj_dir, "formal-syn-" + node)
            formal_par_run_dir = os.path.join(obj_dir, "formal-par-" + node)
            timing_syn_run_dir = os.path.join(obj_dir, "timing-syn-" + node)
            timing_par_run_dir = os.path.join(obj_dir, "timing-par-" + node)

            p_sim_rtl_in = proj_confs
            sim_rtl_out = os.path.join(sim_rtl_run_dir, "sim-output-full.json")
            power_sim_rtl_in = os.path.join(obj_dir, "power-sim-rtl-{}-input.json".format(node))
            #power_rtl_in = os.path.join(obj_dir, "power-rtl-{}-input.json".format(node))
            power_rtl_out = os.path.join(power_rtl_run_dir, "power-output-full.json")
            p_syn_in = proj_confs
            syn_out = os.path.join(syn_run_dir, "syn-output-full.json")
            sim_syn_in = os.path.join(obj_dir, "sim-syn-{}-input.json".format(node))
            sim_syn_out = os.path.join(sim_syn_run_dir, "sim-output-full.json")
            power_sim_syn_in = os.path.join(obj_dir, "power-sim-syn-{}-input.json".format(node))
            power_syn_in = os.path.join(obj_dir, "power-syn-{}-input.json".format(node))
            power_syn_out = os.path.join(power_syn_run_dir, "power-output-full.json")
            par_in = os.path.join(obj_dir, "par-{}-input.json".format(node))
            par_out = os.path.join(par_run_dir, "par-output-full.json")
            sim_par_in = os.path.join(obj_dir, "sim-par-{}-input.json".format(node))
            sim_par_out = os.path.join(sim_par_run_dir, "sim-output-full.json")
            power_sim_par_in = os.path.join(obj_dir, "power-sim-par-{}-input.json".format(node))
            power_par_in = os.path.join(obj_dir, "power-par-{}-input.json".format(node))
            power_par_out = os.path.join(power_par_run_dir, "power-output-full.json")
            drc_in = os.path.join(obj_dir, "drc-{}-input.json".format(node))
            drc_out = os.path.join(drc_run_dir, "drc-output-full.json")
            lvs_in = os.path.join(obj_dir, "lvs-{}-input.json".format(node))
            lvs_out = os.path.join(lvs_run_dir, "lvs-output-full.json")
            formal_syn_in = os.path.join(obj_dir, "formal-syn-{}-input.json".format(node))
            formal_syn_out = os.path.join(formal_syn_run_dir, "formal-output-full.json")
            formal_par_in = os.path.join(obj_dir, "formal-par-{}-input.json".format(node))
            formal_par_out = os.path.join(formal_par_run_dir, "formal-output-full.json")
            timing_syn_in = os.path.join(obj_dir, "timing-syn-{}-input.json".format(node))
            timing_syn_out = os.path.join(timing_syn_run_dir, "timing-output-full.json")
            timing_par_in = os.path.join(obj_dir, "timing-par-{}-input.json".format(node))
            timing_par_out = os.path.join(timing_par_run_dir, "timing-output-full.json")

            # need to revert this each time
            syn_deps = "$(HAMMER_DEPENDENCIES)"
            par_to_syn = ""
            if len(out_edges) > 0:
                syn_deps = os.path.join(obj_dir, "syn-{}-input.json".format(node))
                p_syn_in = "-p {}".format(syn_deps)
                out_confs = [os.path.join(obj_dir, "par-" + x, "par-output-full.json") for x in out_edges]
                prereqs = " ".join(out_confs)
                pstring = " ".join(["-p " + x for x in out_confs])
                par_to_syn = textwrap.dedent("""
                    .PHONY: hier-par-to-syn-{node} redo-hier-par-to-syn-{node}

                    {syn_deps}: {prereqs}
                    \t$(HAMMER_EXEC) {env_confs} {pstring} -o {syn_deps} --obj_dir {obj_dir} hier-par-to-syn

                    hier-par-to-syn-{node}: {syn_deps}

                    redo-hier-par-to-syn-{node}:
                    \t$(HAMMER_EXEC) {env_confs} {pstring} -o {syn_deps} --obj_dir {obj_dir} hier-par-to-syn
                    """.format(syn_deps=syn_deps, prereqs=prereqs, env_confs=env_confs, pstring=pstring,
                    obj_dir=obj_dir, node=node))

            output += make_text.format(suffix="-"+node, mod=node, env_confs=env_confs, obj_dir=obj_dir, syn_deps=syn_deps,
                par_to_syn=par_to_syn,
                p_sim_rtl_in=p_sim_rtl_in, sim_rtl_out=sim_rtl_out, sim_rtl_run_dir=sim_rtl_run_dir,
                power_sim_rtl_in=power_sim_rtl_in, power_rtl_out=power_rtl_out, power_rtl_run_dir=power_rtl_run_dir,
                sim_syn_in=sim_syn_in, sim_syn_out=sim_syn_out, sim_syn_run_dir=sim_syn_run_dir,
                sim_par_in=sim_par_in, sim_par_out=sim_par_out, sim_par_run_dir=sim_par_run_dir,
                p_syn_in=p_syn_in, syn_out=syn_out, par_in=par_in, par_out=par_out,
                power_sim_syn_in=power_sim_syn_in, power_syn_in=power_syn_in, power_syn_out=power_syn_out, power_syn_run_dir=power_syn_run_dir,
                power_sim_par_in=power_sim_par_in, power_par_in=power_par_in, power_par_out=power_par_out, power_par_run_dir=power_par_run_dir,
                drc_in=drc_in, drc_out=drc_out, lvs_in=lvs_in, lvs_out=lvs_out,
                formal_syn_in=formal_syn_in, formal_syn_out=formal_syn_out, formal_syn_run_dir=formal_syn_run_dir,
                formal_par_in=formal_par_in, formal_par_out=formal_par_out, formal_par_run_dir=formal_par_run_dir,
                timing_syn_in=timing_syn_in, timing_syn_out=timing_syn_out, timing_syn_run_dir=timing_syn_run_dir,
                timing_par_in=timing_par_in, timing_par_out=timing_par_out, timing_par_run_dir=timing_par_run_dir)

    with open(makefile, "w") as f:
        f.write(output)

    return dependency_graph

def _airflow_design_name(proj_confs: List[str], top_module: str) -> str:
    for p in proj_confs:
        parts = os.path.realpath(p).split(os.sep)
        if "configs-design" in parts:
            i = parts.index("configs-design")
            if i + 1 < len(parts):
                return parts[i + 1]
    return top_module


_AIRFLOW_DAG_TEMPLATE = '''"""
Auto-generated by hammer-vlsi build (vlsi.core.build_system: airflow).
Regenerate by re-running hammer-vlsi build. Do not hand-edit.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pendulum
from airflow.exceptions import AirflowSkipException

try:
    from airflow.sdk import dag, task, Param
except ImportError:
    from airflow.decorators import dag, task
    from airflow.models.param import Param

try:
    from airflow.task.trigger_rule import TriggerRule
except ImportError:
    from airflow.utils.trigger_rule import TriggerRule


DAG_ID = {dag_id!r}
DESIGN_NAME = {design_name!r}
TOP_MODULE = {top_module!r}
ENV_CONFS = {env_confs!r}
# Per-tool config bundles: {{tools_name: [pdk, tool, design configs...]}}.
# Tools_name matches the file stem of e2e/configs-tool/<name>.yml.
PROJ_CONFS_BY_TOOLS = {proj_confs_by_tools!r}
DEFAULT_TOOLS = {default_tools!r}
TOOLS_CHOICES = sorted(PROJ_CONFS_BY_TOOLS.keys())
OBJ_DIR_DEFAULT = {obj_dir!r}


def _pin_design_env() -> None:
    os.environ["design"] = DESIGN_NAME


def _resolve_proj_confs(tools_choice):
    if tools_choice in PROJ_CONFS_BY_TOOLS:
        return PROJ_CONFS_BY_TOOLS[tools_choice]
    print(f"Unknown tools={{tools_choice!r}}; falling back to {{DEFAULT_TOOLS!r}}")
    return PROJ_CONFS_BY_TOOLS[DEFAULT_TOOLS]


from hammer.shell.hammer_vlsi import AIRFlow, run_cli_driver  # noqa: E402


class AIRFlow_generated(AIRFlow):
    def __init__(self, context=None, proj_confs=None):
        super().__init__(context=context)
        self.proj_confs = (
            proj_confs if proj_confs is not None else PROJ_CONFS_BY_TOOLS[DEFAULT_TOOLS]
        )

    def syn_par(self) -> None:
        sys.argv = [
            "hammer-vlsi",
            "syn_par",
            "--obj_dir", self.OBJ_DIR,
        ]
        for e in ENV_CONFS:
            sys.argv.extend(["-e", e])
        for p in self.proj_confs:
            sys.argv.extend(["-p", p])
        if self.extra:
            sys.argv.extend(["-p", self.extra])
        if self.args:
            sys.argv.extend(self.args.split())
        print("Running:", " ".join(sys.argv))
        run_cli_driver()


@dag(
    dag_id=DAG_ID,
    start_date=pendulum.datetime(2024, 1, 1, tz="America/Los_Angeles"),
    schedule=None,
    catchup=False,
    tags=["hammer", "autogen", DESIGN_NAME],
    params={{
        "clean":   Param(default=False, type="boolean", title="Clean Build Directory"),
        "build":   Param(default=True,  type="boolean", title="Build Design"),
        "syn_par": Param(default=True,  type="boolean", title="Synthesis + Place-and-Route"),
        "tools":   Param(
            default=DEFAULT_TOOLS,
            type="string",
            enum=TOOLS_CHOICES,
            title="Tool config",
            description="Which configs-tool/<name>.yml to use for this run.",
        ),
    }},
    render_template_as_native_obj=True,
    description="Auto-generated Hammer syn_par DAG for " + DESIGN_NAME,
)
def create_generated_dag():
    def get_param(context, name, default=True):
        if name in context.get("dag_run", {{}}).conf:
            return context["dag_run"].conf[name]
        if name in context.get("params", {{}}):
            return context["params"][name]
        return default

    @task.branch(trigger_rule=TriggerRule.ALL_SUCCESS)
    def start(**context):
        has_steps = get_param(context, "build", True) or get_param(context, "syn_par", True)
        if has_steps or get_param(context, "clean", False):
            return "clean"
        return "exit_"

    @task
    def clean(**context):
        _pin_design_env()
        if get_param(context, "clean", False):
            flow = AIRFlow_generated(context=context)
            if os.path.exists(flow.OBJ_DIR):
                subprocess.run(f"rm -rf {{flow.OBJ_DIR}} hammer-vlsi-*.log", shell=True, check=True)

    @task.branch(trigger_rule=TriggerRule.ALL_SUCCESS)
    def build_decider(**context):
        if get_param(context, "build", True):
            return "build"
        if get_param(context, "syn_par", True):
            return "syn_par_decider"
        return "exit_"

    @task
    def build(**context):
        _pin_design_env()
        if get_param(context, "build", True):
            AIRFlow_generated(context=context).build()
        else:
            raise AirflowSkipException("build skipped")

    @task.branch(trigger_rule=TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS)
    def syn_par_decider(**context):
        if get_param(context, "syn_par", True):
            return "syn_par"
        return "exit_"

    @task
    def syn_par(**context):
        _pin_design_env()
        if get_param(context, "syn_par", True):
            tools_choice = get_param(context, "tools", DEFAULT_TOOLS)
            proj_confs = _resolve_proj_confs(tools_choice)
            print(f"Tools choice: {{tools_choice!r}}; first proj_conf={{proj_confs[0]!r}}")
            AIRFlow_generated(context=context, proj_confs=proj_confs).syn_par()
        else:
            raise AirflowSkipException("syn_par skipped")

    @task(trigger_rule=TriggerRule.NONE_FAILED)
    def exit_(**context):
        run_id = None
        try:
            run_id = context["dag_run"].run_id
        except Exception:
            pass
        if not run_id:
            return
        from hammer.vlsi.pd_cache import read_run_cache_summary, clear_run_cache_events
        summary = read_run_cache_summary(run_id)
        if summary:
            print("=" * 72)
            print(f"PD CACHE SUMMARY for run {{run_id}}")
            print("=" * 72)
            print(summary)
            print("=" * 72)
            clear_run_cache_events(run_id)

    start_t = start()
    clean_t = clean()
    build_decide_t = build_decider()
    build_t = build()
    syn_par_decide_t = syn_par_decider()
    syn_par_t = syn_par()
    exit_t = exit_()

    start_t >> [clean_t, exit_t]
    clean_t >> build_decide_t
    build_decide_t >> [build_t, syn_par_decide_t, exit_t]
    build_t >> syn_par_decide_t
    syn_par_decide_t >> [syn_par_t, exit_t]
    syn_par_t >> exit_t


generated_dag = create_generated_dag()
'''


_AIRFLOW_HIER_DAG_TEMPLATE = '''"""
Auto-generated by hammer-vlsi build (vlsi.core.build_system: airflow, hierarchical).
Regenerate by re-running hammer-vlsi build. Do not hand-edit.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pendulum
from airflow.exceptions import AirflowSkipException

try:
    from airflow.sdk import dag, task, Param
except ImportError:
    from airflow.decorators import dag, task
    from airflow.models.param import Param

try:
    from airflow.task.trigger_rule import TriggerRule
except ImportError:
    from airflow.utils.trigger_rule import TriggerRule

from airflow.utils.task_group import TaskGroup


DAG_ID = {dag_id!r}
DESIGN_NAME = {design_name!r}
TOP_MODULE = {top_module!r}
ENV_CONFS = {env_confs!r}
# Per-tool config bundles: {{tools_name: [pdk, tool, design configs...]}}.
# Tools_name matches the file stem of e2e/configs-tool/<name>.yml.
PROJ_CONFS_BY_TOOLS = {proj_confs_by_tools!r}
DEFAULT_TOOLS = {default_tools!r}
TOOLS_CHOICES = sorted(PROJ_CONFS_BY_TOOLS.keys())
OBJ_DIR_DEFAULT = {obj_dir!r}

# {{module: [child, ...]}} — children whose par-output feeds this module's syn input.
# Leaves have empty lists.
DEPENDENCY_GRAPH = {dep_graph!r}


def _pin_design_env() -> None:
    os.environ["design"] = DESIGN_NAME


def _resolve_proj_confs(tools_choice):
    if tools_choice in PROJ_CONFS_BY_TOOLS:
        return PROJ_CONFS_BY_TOOLS[tools_choice]
    print(f"Unknown tools={{tools_choice!r}}; falling back to {{DEFAULT_TOOLS!r}}")
    return PROJ_CONFS_BY_TOOLS[DEFAULT_TOOLS]


from hammer.shell.hammer_vlsi import AIRFlow, run_cli_driver  # noqa: E402


class AIRFlow_hier(AIRFlow):
    def __init__(self, context=None, proj_confs=None):
        super().__init__(context=context)
        self.proj_confs = (
            proj_confs if proj_confs is not None else PROJ_CONFS_BY_TOOLS[DEFAULT_TOOLS]
        )

    def _run(self, action, extra=None, output=None):
        sys.argv = [
            "hammer-vlsi",
            action,
            "--obj_dir", self.OBJ_DIR,
        ]
        for e in ENV_CONFS:
            sys.argv.extend(["-e", e])
        for p in self.proj_confs:
            sys.argv.extend(["-p", p])
        if extra:
            sys.argv.extend(extra)
        if output:
            sys.argv.extend(["-o", output])
        if self.extra:
            sys.argv.extend(["-p", self.extra])
        if self.args:
            sys.argv.extend(self.args.split())
        print("Running:", " ".join(sys.argv))
        run_cli_driver()

    def syn(self, target, with_hier_input=False):
        extras = []
        if with_hier_input:
            extras = ["-p", f"{{self.OBJ_DIR}}/syn-{{target}}-input.json"]
        self._run(f"syn-{{target}}", extra=extras)

    def syn_to_par(self, target):
        syn_out = f"{{self.OBJ_DIR}}/syn-{{target}}/syn-output-full.json"
        par_in = f"{{self.OBJ_DIR}}/par-{{target}}-input.json"
        self._run("syn-to-par", extra=["-p", syn_out], output=par_in)

    def par(self, target):
        par_in = f"{{self.OBJ_DIR}}/par-{{target}}-input.json"
        self._run(f"par-{{target}}", extra=["-p", par_in])

    def hier_par_to_syn(self, target, children):
        extras = []
        for c in children:
            extras.extend(["-p", f"{{self.OBJ_DIR}}/par-{{c}}/par-output-full.json"])
        syn_in = f"{{self.OBJ_DIR}}/syn-{{target}}-input.json"
        self._run("hier-par-to-syn", extra=extras, output=syn_in)

    def par_to_drc(self, target):
        par_out = f"{{self.OBJ_DIR}}/par-{{target}}/par-output-full.json"
        drc_in = f"{{self.OBJ_DIR}}/drc-{{target}}-input.json"
        self._run("par-to-drc", extra=["-p", par_out], output=drc_in)

    def drc(self, target):
        drc_in = f"{{self.OBJ_DIR}}/drc-{{target}}-input.json"
        self._run(f"drc-{{target}}", extra=["-p", drc_in])

    def par_to_lvs(self, target):
        par_out = f"{{self.OBJ_DIR}}/par-{{target}}/par-output-full.json"
        lvs_in = f"{{self.OBJ_DIR}}/lvs-{{target}}-input.json"
        self._run("par-to-lvs", extra=["-p", par_out], output=lvs_in)

    def lvs(self, target):
        lvs_in = f"{{self.OBJ_DIR}}/lvs-{{target}}-input.json"
        self._run(f"lvs-{{target}}", extra=["-p", lvs_in])


@dag(
    dag_id=DAG_ID,
    start_date=pendulum.datetime(2024, 1, 1, tz="America/Los_Angeles"),
    schedule=None,
    catchup=False,
    tags=["hammer", "autogen", "hier", DESIGN_NAME],
    params={{
        "clean":   Param(default=False, type="boolean", title="Clean Build Directory"),
        "build":   Param(default=True,  type="boolean", title="Build Design"),
        "syn":     Param(default=True,  type="boolean", title="Synthesis (all modules)"),
        "par":     Param(default=True,  type="boolean", title="Place and Route (all modules)"),
        "drc":     Param(default=False, type="boolean", title="DRC (top module only)"),
        "lvs":     Param(default=False, type="boolean", title="LVS (top module only)"),
        "tools":   Param(
            default=DEFAULT_TOOLS,
            type="string",
            enum=TOOLS_CHOICES,
            title="Tool config",
            description="Which configs-tool/<name>.yml to use for this run.",
        ),
    }},
    render_template_as_native_obj=True,
    description="Auto-generated Hammer hierarchical DAG for " + DESIGN_NAME,
)
def create_generated_dag():
    def gp(context, name, default=True):
        if name in context.get("dag_run", {{}}).conf:
            return context["dag_run"].conf[name]
        if name in context.get("params", {{}}):
            return context["params"][name]
        return default

    def _flow(context):
        tools_choice = gp(context, "tools", DEFAULT_TOOLS)
        proj_confs = _resolve_proj_confs(tools_choice)
        return AIRFlow_hier(context=context, proj_confs=proj_confs)

    @task.branch(trigger_rule=TriggerRule.ALL_SUCCESS)
    def start(**context):
        if gp(context, "clean", False):
            return "clean"
        if gp(context, "build", True) or gp(context, "syn", True) or gp(context, "par", True):
            return "build"
        return "exit_"

    @task
    def clean(**context):
        _pin_design_env()
        if gp(context, "clean", False):
            flow = _flow(context)
            if os.path.exists(flow.OBJ_DIR):
                subprocess.run(f"rm -rf {{flow.OBJ_DIR}} hammer-vlsi-*.log", shell=True, check=True)

    @task(trigger_rule=TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS)
    def build(**context):
        _pin_design_env()
        if gp(context, "build", True):
            _flow(context).build()
        else:
            raise AirflowSkipException("build skipped")

    @task(trigger_rule=TriggerRule.NONE_FAILED)
    def exit_(**context):
        run_id = None
        try:
            run_id = context["dag_run"].run_id
        except Exception:
            pass
        if not run_id:
            return
        from hammer.vlsi.pd_cache import read_run_cache_summary, clear_run_cache_events
        summary = read_run_cache_summary(run_id)
        if summary:
            print("=" * 72)
            print(f"PD CACHE SUMMARY for run {{run_id}}")
            print("=" * 72)
            print(summary)
            print("=" * 72)
            clear_run_cache_events(run_id)

    def _make_syn(module, has_children):
        @task(task_id=f"syn_{{module}}", trigger_rule=TriggerRule.NONE_FAILED)
        def _t(**context):
            _pin_design_env()
            if not gp(context, "syn", True):
                raise AirflowSkipException(f"syn skipped for {{module}}")
            _flow(context).syn(module, with_hier_input=has_children)
        return _t

    def _make_syn_to_par(module):
        @task(task_id=f"syn_to_par_{{module}}", trigger_rule=TriggerRule.NONE_FAILED)
        def _t(**context):
            _pin_design_env()
            if not gp(context, "par", True):
                raise AirflowSkipException(f"syn-to-par skipped for {{module}}")
            _flow(context).syn_to_par(module)
        return _t

    def _make_par(module):
        @task(task_id=f"par_{{module}}", trigger_rule=TriggerRule.NONE_FAILED)
        def _t(**context):
            _pin_design_env()
            if not gp(context, "par", True):
                raise AirflowSkipException(f"par skipped for {{module}}")
            _flow(context).par(module)
        return _t

    def _make_hps(module, children):
        @task(task_id=f"hier_par_to_syn_{{module}}", trigger_rule=TriggerRule.NONE_FAILED)
        def _t(**context):
            _pin_design_env()
            if not gp(context, "syn", True):
                raise AirflowSkipException(f"hier-par-to-syn skipped for {{module}}")
            _flow(context).hier_par_to_syn(module, children)
        return _t

    def _make_par_to_drc(module):
        @task(task_id=f"par_to_drc_{{module}}", trigger_rule=TriggerRule.NONE_FAILED)
        def _t(**context):
            _pin_design_env()
            if not gp(context, "drc", False):
                raise AirflowSkipException(f"par-to-drc skipped for {{module}}")
            _flow(context).par_to_drc(module)
        return _t

    def _make_drc(module):
        @task(task_id=f"drc_{{module}}", trigger_rule=TriggerRule.NONE_FAILED)
        def _t(**context):
            _pin_design_env()
            if not gp(context, "drc", False):
                raise AirflowSkipException(f"drc skipped for {{module}}")
            _flow(context).drc(module)
        return _t

    def _make_par_to_lvs(module):
        @task(task_id=f"par_to_lvs_{{module}}", trigger_rule=TriggerRule.NONE_FAILED)
        def _t(**context):
            _pin_design_env()
            if not gp(context, "lvs", False):
                raise AirflowSkipException(f"par-to-lvs skipped for {{module}}")
            _flow(context).par_to_lvs(module)
        return _t

    def _make_lvs(module):
        @task(task_id=f"lvs_{{module}}", trigger_rule=TriggerRule.NONE_FAILED)
        def _t(**context):
            _pin_design_env()
            if not gp(context, "lvs", False):
                raise AirflowSkipException(f"lvs skipped for {{module}}")
            _flow(context).lvs(module)
        return _t

    start_t = start()
    clean_t = clean()
    build_t = build()
    exit_t = exit_()

    start_t >> [clean_t, exit_t]
    clean_t >> build_t

    # Build one TaskGroup per module from the dep graph
    tasks = {{}}
    for module, children in DEPENDENCY_GRAPH.items():
        with TaskGroup(group_id=module):
            d = {{}}
            d["syn"] = _make_syn(module, bool(children))()
            d["syn_to_par"] = _make_syn_to_par(module)()
            d["par"] = _make_par(module)()
            if children:
                d["hier_par_to_syn"] = _make_hps(module, children)()
                d["hier_par_to_syn"] >> d["syn"]
            d["syn"] >> d["syn_to_par"] >> d["par"]
        tasks[module] = d

    # Hier wiring: each non-leaf's hier_par_to_syn depends on all children's par.
    for module, children in DEPENDENCY_GRAPH.items():
        if children:
            hps = tasks[module]["hier_par_to_syn"]
            for child in children:
                tasks[child]["par"] >> hps

    # build feeds every leaf module's syn (modules with no children).
    for module, children in DEPENDENCY_GRAPH.items():
        if not children:
            build_t >> tasks[module]["syn"]

    # Top module's par optionally fans into DRC and LVS branches.
    top_par = tasks[TOP_MODULE]["par"]
    with TaskGroup(group_id=f"{{TOP_MODULE}}_post_par"):
        ptd = _make_par_to_drc(TOP_MODULE)()
        drc_t = _make_drc(TOP_MODULE)()
        ptl = _make_par_to_lvs(TOP_MODULE)()
        lvs_t = _make_lvs(TOP_MODULE)()
        top_par >> ptd >> drc_t
        top_par >> ptl >> lvs_t
        [drc_t, lvs_t] >> exit_t

    top_par >> exit_t


generated_dag = create_generated_dag()
'''


def _build_airflow_dag_flat(
    driver: HammerDriver,
    append_error_func: Callable[[str], None],
    obj_dir: str,
    top_module: str,
    env_confs: List[str],
    proj_confs_by_tools: Dict[str, List[str]],
    default_tools: str,
    design_name: str,
    dag_id: str,
) -> None:
    dag_text = _AIRFLOW_DAG_TEMPLATE.format(
        dag_id=dag_id,
        design_name=design_name,
        top_module=top_module,
        env_confs=env_confs,
        proj_confs_by_tools=proj_confs_by_tools,
        default_tools=default_tools,
        obj_dir=obj_dir,
    )
    _write_dag(dag_file_for(obj_dir), dag_text, design_name, dag_id, driver, append_error_func)


def _build_airflow_dag_hier(
    driver: HammerDriver,
    append_error_func: Callable[[str], None],
    obj_dir: str,
    top_module: str,
    env_confs: List[str],
    proj_confs_by_tools: Dict[str, List[str]],
    default_tools: str,
    design_name: str,
    dag_id: str,
    dependency_graph: dict,
) -> None:
    # Reduce (in_edges, out_edges) tuples to {module: [child, ...]}.
    dep_graph = {node: list(edges[1]) for node, edges in dependency_graph.items()}
    if top_module not in dep_graph:
        append_error_func(
            f"top module {top_module!r} not in hierarchical dependency graph: "
            f"{sorted(dep_graph.keys())}"
        )
    dag_text = _AIRFLOW_HIER_DAG_TEMPLATE.format(
        dag_id=dag_id,
        design_name=design_name,
        top_module=top_module,
        env_confs=env_confs,
        proj_confs_by_tools=proj_confs_by_tools,
        default_tools=default_tools,
        obj_dir=obj_dir,
        dep_graph=dep_graph,
    )
    _write_dag(dag_file_for(obj_dir), dag_text, design_name, dag_id, driver, append_error_func)


def dag_file_for(obj_dir: str) -> str:
    return os.path.join(obj_dir, "airflow_dag.py")


def _write_dag(
    dag_file: str,
    dag_text: str,
    design_name: str,
    dag_id: str,
    driver: HammerDriver,
    append_error_func: Callable[[str], None],
) -> None:
    os.makedirs(os.path.dirname(dag_file), exist_ok=True)
    with open(dag_file, "w") as f:
        f.write(dag_text)

    link_target = driver.database.get_setting("vlsi.core.airflow_dags_folder", nullvalue="")
    if link_target:
        link_path = os.path.join(link_target, f"hammer_{design_name}_dag.py")
        try:
            if os.path.islink(link_path) or os.path.exists(link_path):
                os.remove(link_path)
            os.symlink(dag_file, link_path)
            print(f"Linked DAG: {link_path} -> {dag_file}")
        except Exception as e:
            append_error_func(f"Could not symlink DAG into {link_target}: {e}")
    else:
        print(f"DAG written: {dag_file}")
        print(f"dag_id: {dag_id}")
        print(f"To register with Airflow, symlink it into $AIRFLOW_HOME/dags/:")
        print(f"  ln -sf {dag_file} $AIRFLOW_HOME/dags/hammer_{design_name}_dag.py")
        print(f"Or set vlsi.core.airflow_dags_folder in your config and re-run build.")


def build_airflow_dag(
    driver: HammerDriver,
    append_error_func: Callable[[str], None],
    proj_confs_by_tools: Optional[Dict[str, List[str]]] = None,
    default_tools: Optional[str] = None,
) -> dict:
    dependency_graph = driver.get_hierarchical_dependency_graph()

    obj_dir = os.path.realpath(driver.obj_dir)
    top_module = str(driver.database.get_setting("synthesis.inputs.top_module"))
    env_confs = [os.path.realpath(x) for x in driver.options.environment_configs]
    proj_confs = [os.path.realpath(x) for x in driver.options.project_configs]
    design_name = _airflow_design_name(proj_confs, top_module)
    dag_id = driver.database.get_setting("vlsi.core.airflow_dag_id", nullvalue="") \
        or f"Hammer_{design_name}"

    # When the caller (e.g. hammer-pd-store make-dag) didn't pass an explicit
    # per-tool bundle, fall back to a single-option dict so the generated DAG
    # still has the `tools` dropdown but only one choice in it.
    if proj_confs_by_tools is None:
        proj_confs_by_tools = {"default": proj_confs}
        default_tools = "default"
    elif default_tools is None or default_tools not in proj_confs_by_tools:
        default_tools = sorted(proj_confs_by_tools.keys())[0]

    if dependency_graph:
        hier_top = driver.database.get_setting(
            "vlsi.inputs.hierarchical.top_module", nullvalue=""
        ) or top_module
        _build_airflow_dag_hier(
            driver, append_error_func, obj_dir, hier_top,
            env_confs, proj_confs_by_tools, default_tools,
            design_name, dag_id, dependency_graph,
        )
    else:
        _build_airflow_dag_flat(
            driver, append_error_func, obj_dir, top_module,
            env_confs, proj_confs_by_tools, default_tools,
            design_name, dag_id,
        )

    return dependency_graph


BuildSystems = {
    "make":    build_makefile,
    "airflow": build_airflow_dag,
    "none":    build_noop
}  # type: Dict[str, Callable[[HammerDriver, Callable[[str], None]], dict]]
