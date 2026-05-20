#  hammer-vlsi plugin for Cadence Innovus+ Synthesis.
#  Runs RTL synthesis using Innovus -stylus -synthesis (Innovus+ flow).
#  Requires Innovus 25.1+ and the INVS500 license.
#
#  Python-native script generation (syn.py) is enabled automatically when
#  the tool version is >= 25.1.  Older versions fall back to syn.tcl as before.
#
#  See LICENSE for licence details.

from hammer.vlsi import HammerTool, HammerToolStep, HammerToolHookAction, HierarchicalMode
from hammer.vlsi import HammerSynthesisTool, PlacementConstraintType
from hammer.logging import HammerVLSILogging
from hammer.vlsi import MMMCCornerType
import hammer.tech as hammer_tech

from typing import Dict, List, Any, Optional

from hammer.tech.specialcells import CellType

import os

from hammer.common.cadence import CadenceTool

# Import the converter from the PAR plugin so we share identical translation logic.
# Adjust the import path to match wherever the PAR plugin lives in your tree.
try:
    from hammer.par.innovus import InnovusTclToPythonConverter  # type: ignore
except ImportError:
    # Fallback: define a minimal stub so the plugin still loads in TCL-only mode.
    class InnovusTclToPythonConverter:  # type: ignore
        def convert_line(self, line: str) -> str:
            return f"# CONVERTER_UNAVAILABLE: {line}"


class InnovusPlus(HammerSynthesisTool, CadenceTool):

    # -------------------------------------------------------------------------
    # Python-native generation (25.1+)
    # -------------------------------------------------------------------------

    @property
    def use_python(self) -> bool:
        """True when targeting Innovus 25.1+ Python API."""
        return self.version() >= self.version_number("251")

    def py_append(self, python_line: str) -> None:
        """Append a raw Python line directly to the output (25.1+ only)."""
        self.output.append(python_line)

    def append(self, cmd: str, clean: bool = False) -> None:
        """Append a command.  In Python mode, converts TCL to Python."""
        if self.use_python:
            converter = InnovusTclToPythonConverter()
            for line in cmd.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                converted = converter.convert_line(line)
                if converted:
                    self.output.append(converted)
        else:
            super().append(cmd, clean=clean)  # type: ignore

    def verbose_append(self, cmd: str, clean: bool = False) -> None:
        """Append a command with verbose echo.  In Python mode, converts TCL to Python."""
        if self.use_python:
            converter = InnovusTclToPythonConverter()
            for line in cmd.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                converted = converter.convert_line(line)
                if converted:
                    self.output.append(converted)
        else:
            super().verbose_append(cmd, clean=clean)  # type: ignore

    # -------------------------------------------------------------------------
    # Standard plugin boilerplate
    # -------------------------------------------------------------------------

    @property
    def post_synth_sdc(self) -> Optional[str]:
        return None

    def fill_outputs(self) -> bool:
        # Check that the regs paths were written properly if the write_regs step was run
        self.output_seq_cells = self.all_cells_path
        self.output_all_regs = self.all_regs_path
        if self.ran_write_regs:
            if not os.path.isfile(self.all_cells_path):
                raise ValueError("Output find_regs_cells.json %s not found" % (self.all_cells_path))
            if not os.path.isfile(self.all_regs_path):
                raise ValueError("Output find_regs_paths.json %s not found" % (self.all_regs_path))
            if not self.process_reg_paths(self.all_regs_path):
                self.logger.error("Failed to process all register paths")
        else:
            self.logger.info("Did not run write_regs")

        self.output_files = [self.mapped_v_path]
        self.output_sdc = self.mapped_sdc_path
        self.sdf_file = self.output_sdf_path

        if self.ran_write_outputs:
            if not os.path.isfile(self.mapped_v_path):
                raise ValueError("Output mapped verilog %s not found" % (self.mapped_v_path))
            if not os.path.isfile(self.mapped_sdc_path):
                raise ValueError("Output SDC %s not found" % (self.mapped_sdc_path))
            if not os.path.isfile(self.output_sdf_path):
                raise ValueError("Output SDF %s not found" % (self.output_sdf_path))
        else:
            self.logger.info("Did not run write_outputs")

        return True

    @property
    def env_vars(self) -> Dict[str, str]:
        new_dict = dict(super().env_vars)
        new_dict["INNOVUS_BIN"] = self.get_setting("synthesis.innovus_plus.innovus_bin")
        return new_dict

    def export_config_outputs(self) -> Dict[str, Any]:
        outputs = dict(super().export_config_outputs())
        outputs["synthesis.outputs.sdc"] = self.output_sdc
        outputs["synthesis.outputs.seq_cells"] = self.output_seq_cells
        outputs["synthesis.outputs.all_regs"] = self.output_all_regs
        outputs["synthesis.outputs.sdf_file"] = self.output_sdf_path
        return outputs

    def tool_config_prefix(self) -> str:
        return "synthesis.innovus_plus"

    def get_tool_hooks(self) -> List[HammerToolHookAction]:
        return [self.make_persistent_hook(innovus_plus_global_settings)]

    @property
    def steps(self) -> List[HammerToolStep]:
        steps_methods = [
            self.init_environment,
            self.syn_generic,
            self.syn_map,
            self.write_regs,
            self.generate_reports,
            self.write_outputs,
            self.run_innovus_plus,
        ]
        return self.make_steps_from_methods(steps_methods)

    def do_pre_steps(self, first_step: HammerToolStep) -> bool:
        assert super().do_pre_steps(first_step)
        return True

    def do_between_steps(self, prev: HammerToolStep, next: HammerToolStep) -> bool:
        assert super().do_between_steps(prev, next)
        return True

    def do_post_steps(self) -> bool:
        assert super().do_post_steps()
        return True

    # -------------------------------------------------------------------------
    # Path helpers
    # -------------------------------------------------------------------------

    @property
    def mapped_v_path(self) -> str:
        return os.path.join(self.run_dir, "{}.mapped.v".format(self.top_module))

    @property
    def mapped_sdc_path(self) -> str:
        return os.path.join(self.run_dir, "{}.mapped.sdc".format(self.top_module))

    @property
    def all_regs_path(self) -> str:
        return os.path.join(self.run_dir, "find_regs_paths.json")

    @property
    def all_cells_path(self) -> str:
        return os.path.join(self.run_dir, "find_regs_cells.json")

    @property
    def output_sdf_path(self) -> str:
        return os.path.join(self.run_dir, "{top}.mapped.sdf".format(top=self.top_module))

    @property
    def ran_write_regs(self) -> bool:
        return self.attr_getter("_ran_write_regs", False)

    @ran_write_regs.setter
    def ran_write_regs(self, val: bool) -> None:
        self.attr_setter("_ran_write_regs", val)

    @property
    def ran_write_outputs(self) -> bool:
        return self.attr_getter("_ran_write_outputs", False)

    @ran_write_outputs.setter
    def ran_write_outputs(self, val: bool) -> None:
        self.attr_setter("_ran_write_outputs", val)

    # -------------------------------------------------------------------------
    # Python-native helpers (25.1+)
    # -------------------------------------------------------------------------

    def _generate_dont_use_commands_python(self) -> None:
        """Python-native equivalent of generate_dont_use_commands() for Innovus 25.1+."""
        for cell in self.get_dont_use_list():
            if not cell.startswith("*/"):
                cell = "*/" + cell
            self.py_append(f"# set_dont_use {cell}")
            self.py_append(f"_cells = db().lib_cells('{cell}')")
            self.py_append(f"if _cells:")
            self.py_append(f"    set_dont_use(_cells)")
            self.py_append(f"else:")
            self.py_append(f"    print('WARNING: cell {cell} was not found for set_dont_use')")
            self.py_append("")

    def _write_regs_python(self) -> None:
        """Python-native equivalent of write_regs_tcl() for Innovus 25.1+."""
        cells_path = self.all_cells_path
        regs_path  = self.all_regs_path

        # Write sequential cell names to find_regs_cells.json
        self.py_append("# Write sequential cell names")
        self.py_append("import json as _json")
        self.py_append("_refs = db().lib_cells().filter(lambda x: x.is_sequential).base_name")
        self.py_append(
            f"with open({repr(cells_path)}, 'w') as _f:\n"
            f"    _json.dump(list(_refs), _f, indent=4)"
        )
        self.py_append("")

        # Write register output pin paths to find_regs_paths.json
        self.py_append("# Write register output pin paths")
        self.py_append("_reg_pins = all_registers(edge_triggered=True, output_pins=True).to_db_list()")
        self.py_append("_regs = [x.name for x in _reg_pins if x.direction == 'out']")
        self.py_append(
            f"with open({repr(regs_path)}, 'w') as _f:\n"
            f"    _json.dump(_regs, _f, indent=4)"
        )
        self.py_append("")

    # -------------------------------------------------------------------------
    # Steps
    # -------------------------------------------------------------------------

    def init_environment(self) -> bool:
        """
        Set up the Innovus+ environment. Unlike Genus, Innovus+ uses:
          - read_mmmc  (same as Genus/Innovus)
          - read_physical -lefs (same)
          - elaborate_design -script <rtl_script>  (replaces read_hdl + elaborate)
          - init_design
        The RTL elaboration script is auto-generated and written to elab.tcl.
        """
        # Global settings
        if self.use_python:
            self.py_append("db().max_cpus_per_server = {}".format(
                self.get_setting("vlsi.core.max_threads")))
            self.py_append("db().auto_ungroup = 'none'")
            self.py_append("db().hdl_error_on_blackbox = True")
        else:
            self.verbose_append("set_db max_cpus_per_server {}".format(
                self.get_setting("vlsi.core.max_threads")))
            self.verbose_append("set_db auto_ungroup none")
            self.verbose_append("set_db hdl_error_on_blackbox true")

        # Clock gating
        if self.get_setting("synthesis.clock_gating_mode") == "auto":
            if self.use_python:
                self.py_append("db().lp_insert_clock_gating = True")
            else:
                self.verbose_append("set_db lp_insert_clock_gating true")

        # Read MMMC (reuse Hammer's existing MMMC generation)
        mmmc_path = os.path.join(self.run_dir, "mmmc.tcl")
        self.write_contents_to_path(self.generate_mmmc_script(), mmmc_path)
        if self.use_python:
            self.py_append("read_mmmc({!r})".format(mmmc_path))
        else:
            self.verbose_append("read_mmmc {mmmc_path}".format(mmmc_path=mmmc_path))

        # Read LEF layouts
        lef_files = self.technology.read_libs([
            hammer_tech.filters.lef_filter
        ], hammer_tech.HammerTechnologyUtils.to_plain_item)
        if self.use_python:
            lef_str = ", ".join(repr(f) for f in lef_files)
            self.py_append("read_physical(lefs=[{}])".format(lef_str))
        else:
            self.verbose_append("read_physical -lefs {{ {files} }}".format(
                files=" ".join(lef_files)
            ))

        # Generate RTL elaboration script
        if not self.check_input_files([".v", ".sv", ".vh"]):
            return False
        abspath_input_files = list(map(
            lambda name: os.path.join(os.getcwd(), name), self.input_files))

        # Add verilog_synth wrappers from tech (e.g. SRAM wrappers)
        abspath_input_files += self.technology.read_libs([
            hammer_tech.filters.verilog_synth_filter
        ], hammer_tech.HammerTechnologyUtils.to_plain_item)

        defines = " ".join([
            "-define " + d
            for d in self.get_setting("synthesis.inputs.defines", [])
        ])

        elab_tcl_lines = [
            "read_hdl {defines} -sv {{ {files} }}".format(
                defines=defines,
                files=" ".join(abspath_input_files)
            ),
            "elaborate {}".format(self.top_module),
        ]
        elab_tcl_path = os.path.join(self.run_dir, "elab.tcl")
        self.write_contents_to_path("\n".join(elab_tcl_lines), elab_tcl_path)

        # elaborate_design always takes a TCL script path — same call in both modes
        if self.use_python:
            self.py_append("elaborate_design(script={!r})".format(elab_tcl_path))
        else:
            self.verbose_append("elaborate_design -script {elab_tcl}".format(
                elab_tcl=elab_tcl_path))

        # Setup power intent from CPF/UPF if specified.
        # commit_power_intent is called automatically by init_design in Innovus+.
        power_cmds = self.generate_power_spec_commands()
        for cmd in power_cmds:
            if "commit_power_intent" not in cmd:
                self.append(cmd)   # append() handles mode dispatch via converter

        # Initialize the design (loads SDCs, commits power intent)
        if self.use_python:
            self.py_append("init_design()")
        else:
            self.verbose_append("init_design")

        # Set "don't use" cells
        if self.use_python:
            self._generate_dont_use_commands_python()
        else:
            for l in self.generate_dont_use_commands():
                self.append(l)

        # Physical flow: generate floorplan estimate if effort > none
        phys_effort = self.get_setting("synthesis.innovus_plus.phys_flow_effort").lower()
        if phys_effort != "none":
            if self.use_python:
                self.py_append("floorplan_design(prepare=True)")
            else:
                self.verbose_append("floorplan_design -prepare")

        return True

    def syn_generic(self) -> bool:
        """Run generic synthesis (RTL -> generic netlist)."""
        phys_effort = self.get_setting("synthesis.innovus_plus.phys_flow_effort").lower()
        if phys_effort == "none":
            if self.use_python:
                self.py_append("synthesize_design(generic=True)")
            else:
                self.verbose_append("synthesize_design -generic")
        # Physical flow: syn_map handles it via synthesize_design(physical=True)
        return True

    def syn_map(self) -> bool:
        """Run mapping synthesis (generic netlist -> mapped netlist)."""
        phys_effort = self.get_setting("synthesis.innovus_plus.phys_flow_effort").lower()
        if phys_effort == "none":
            if self.use_python:
                self.py_append("synthesize_design(map=True)")
            else:
                self.verbose_append("synthesize_design -map")
        else:
            # synthesize_design -physical does generic + map + physical opt in one shot
            if self.use_python:
                self.py_append("synthesize_design(physical=True)")
            else:
                self.verbose_append("synthesize_design -physical")
        return True

    def write_regs(self) -> bool:
        """Write register info for simulation register forcing."""
        if self.use_python:
            self._write_regs_python()
        else:
            self.append(self.write_regs_tcl())
        self.ran_write_regs = True
        return True

    def generate_reports(self) -> bool:
        """Generate timing and area reports."""
        if self.use_python:
            self.py_append("import os as _os; _os.makedirs('reports', exist_ok=True)")
            # report_* commands use TCL > redirect syntax — use tcl.eval() to preserve that
            self.py_append("tcl.eval('report_timing > reports/final_timing.rpt')")
            self.py_append("tcl.eval('report_area > reports/final_area.rpt')")
            self.py_append("tcl.eval('report_power > reports/final_power.rpt')")
            self.py_append("tcl.eval('report_timing -unconstrained -max_paths 50 > reports/final_unconstrained.rpt')")
        else:
            self.verbose_append("file mkdir reports")
            self.verbose_append("report_timing > reports/final_timing.rpt")
            self.verbose_append("report_area > reports/final_area.rpt")
            self.verbose_append("report_power > reports/final_power.rpt")
            self.verbose_append(
                "report_timing -unconstrained -max_paths 50 > reports/final_unconstrained.rpt")
        return True

    def write_outputs(self) -> bool:
        """Write netlist, SDC, SDF, and DB outputs."""
        top = self.top_module
        corners = self.get_mmmc_corners()

        # Write mapped netlist
        if self.use_python:
            self.py_append("write_netlist({!r})".format(self.mapped_v_path))
        else:
            self.verbose_append("write_netlist {netlist}".format(netlist=self.mapped_v_path))

        # Write SDC
        if corners:
            view_name = "{cname}.setup_view".format(
                cname=next(filter(lambda c: c.type is MMMCCornerType.Setup, corners)).name)
        else:
            view_name = "my_view"
        # Synthesis specific command, Innovus python engine does not seem to support, use tcl.eval()
        if self.use_python:
            self.py_append("tcl.eval('write_sdc -view {view} {path}')".format(
                view=view_name, path=self.mapped_sdc_path))
        else:
            self.verbose_append("write_sdc -view {view} {file}".format(
                view=view_name, file=self.mapped_sdc_path))

        # Write SDF
        if corners:
            max_view = next((c for c in corners if c.type is MMMCCornerType.Setup), None)
            min_view = next((c for c in corners if c.type is MMMCCornerType.Hold), None)
            typ_view = next((c for c in corners if c.type is MMMCCornerType.Extra), None)
            if self.use_python:
                sdf_kwargs = ""
                if max_view:
                    sdf_kwargs += ", max_view={!r}".format("{}.setup_view".format(max_view.name))
                if min_view:
                    sdf_kwargs += ", min_view={!r}".format("{}.hold_view".format(min_view.name))
                if typ_view:
                    sdf_kwargs += ", typical_view={!r}".format("{}.extra_view".format(typ_view.name))
                self.py_append("write_sdf({!r}{})".format(self.output_sdf_path, sdf_kwargs))
            else:
                max_flag = "-max_view {}.setup_view".format(max_view.name) if max_view else ""
                min_flag = "-min_view {}.hold_view".format(min_view.name) if min_view else ""
                typ_flag = "-typical_view {}.extra_view".format(typ_view.name) if typ_view else ""
                self.verbose_append("write_sdf {max} {min} {typ} {run_dir}/{top}.mapped.sdf".format(
                    max=max_flag, min=min_flag, typ=typ_flag,
                    run_dir=self.run_dir, top=top))
        else:
            if self.use_python:
                self.py_append("write_sdf({!r})".format(self.output_sdf_path))
            else:
                self.verbose_append("write_sdf {run_dir}/{top}.mapped.sdf".format(
                    run_dir=self.run_dir, top=top))

        # Write DB for handover to Innovus PAR
        if self.use_python:
            self.py_append("write_db({!r})".format(top))
        else:
            self.verbose_append("write_db {top}".format(top=top))

        self.ran_write_outputs = True
        return True

    def run_innovus_plus(self) -> bool:
        """Close out the synthesis script and run Innovus+."""
        if self.use_python:
            self.py_append("exit()")
        else:
            self.verbose_append("exit")

        if self.use_python:
            # Write Python script — syn.py
            syn_py_filename = os.path.join(self.run_dir, "syn.py")
            header = [
                "# " + "=" * 78,
                "# Generated natively for Innovus 25.1 Python API by HAMMER",
                "# " + "=" * 78,
                "",
            ]
            self.write_contents_to_path(
                "\n".join(header + self.output), syn_py_filename)
            self.logger.info(
                "Generated syn.py natively for Innovus {}".format(self.version()))

            # Wrap in a shell script so LD_LIBRARY_PATH is set before Innovus
            # launches its Python engine (libffi.so.6 must be found at startup).
            wrapper_sh = os.path.join(self.run_dir, "run_syn.sh")
            with open(wrapper_sh, "w") as f:
                f.write(
                    "#!/bin/bash\n"
                    "export LD_LIBRARY_PATH="
                    "/tools/cadence/DDI/DDI251/INNOVUS251/tools.lnx86/cdsgcc/gcc/12.3/install/lib64"
                    ":$LD_LIBRARY_PATH\n"
                    "exec {bin} -stylus -synthesis -no_gui -python"
                    " -files {py} -log {log} -overwrite\n".format(
                        bin=self.get_setting("synthesis.innovus_plus.innovus_bin"),
                        py=syn_py_filename,
                        log=os.path.join(self.run_dir, "innovus_plus_syn.log"),
                    )
                )
            os.chmod(wrapper_sh, 0o755)
            args = [wrapper_sh]
        else:
            # Write TCL script — syn.tcl (legacy path)
            syn_tcl_filename = os.path.join(self.run_dir, "syn.tcl")
            self.write_contents_to_path("\n".join(self.output), syn_tcl_filename)

            args = [
                self.get_setting("synthesis.innovus_plus.innovus_bin"),
                "-stylus",
                "-synthesis",
                "-no_gui",
                "-files", syn_tcl_filename,
                "-log", os.path.join(self.run_dir, "innovus_plus_syn.log"),
                "-overwrite",
            ]

        if bool(self.get_setting("synthesis.innovus_plus.generate_only")):
            self.logger.info("Generate-only mode: command-line is " + " ".join(args))
        else:
            HammerVLSILogging.enable_colour = False
            HammerVLSILogging.enable_tag = False
            self.run_executable(args, cwd=self.run_dir)
            HammerVLSILogging.enable_colour = True
            HammerVLSILogging.enable_tag = True

        return True


def innovus_plus_global_settings(ht: HammerTool) -> bool:
    """Settings that need to be reapplied at every tool invocation."""
    assert isinstance(ht, HammerSynthesisTool)
    assert isinstance(ht, CadenceTool)
    assert isinstance(ht, InnovusPlus)
    ht.create_enter_script()

    max_threads = ht.get_setting("vlsi.core.max_threads")
    if ht.use_python:
        ht.py_append(f"set_multi_cpu_usage(local_cpu={max_threads})")
    else:
        ht.verbose_append("set_multi_cpu_usage -local_cpu {}".format(max_threads))

    return True


tool = InnovusPlus