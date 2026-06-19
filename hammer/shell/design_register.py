"""
Generate Hammer configs from a directory of RTL.

Point this at a project, tell it the top module and clock, and you get back
the four YAML files (common, syn, sky130, par) plus blackbox stubs for any
SRAMs the design uses. The goal is to skip the part where you spend a few
hours discovering that Genus needs the SRAM as a blackbox, par needs the
LEF/lib wired up via extra_libraries, the behavioral SRAM .v file will get
synthesized into a wall of flip-flops if you're not careful, and so on.
Everything in here came out of debugging spring_asic_final_project the hard
way over six attempts.

The flow when you run design-register:

1. Walk the RTL and find which modules the design defines vs which ones
   it just instantiates. The difference is what needs a stub.
2. Recognize sram22_<depth>x<width>m<mux>w<wmask> names and look up the
   matching directory under BWRC's sram22 install. Each one ships a LEF,
   a lib (.lib), a GDS, and a Verilog sim model.
3. Write a blackbox Verilog stub for each SRAM. Just the port list, no
   body. Genus then leaves the SRAM instance alone instead of trying to
   compile its `reg mem[...]` storage into flip-flops.
4. Emit the four configs. sky130.yml gets a vlsi.technology.extra_libraries
   block so Innovus actually sees the SRAM macros at place_inst time.
   par.yml only contains a toplevel placement; you still need to drop in
   x/y for each macro instance by hand.

Stuff this doesn't handle yet:

  - Auto-floorplanning macros. The tool warns when there are macros in
    the design but doesn't try to pick coordinates. That's the next
    feature (design-suggest-placements).
  - PDKs other than sky130.
  - OpenRAM-style SRAMs (sky130_sram_*). Only sram22 today.
  - Generating the Airflow DAG file. You still copy an existing one and
    sed the design name through it. Solved by the planned DAG factory.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


# BWRC ships its sram22 macros under one common root with one directory
# per macro. Each directory has the LEF / lib / GDS / Verilog file we need.
BWRC_SRAM22_ROOT = "/tools/commercial/skywater/local/sram22_sky130_macros"

# sram22 names look like sram22_<depth>x<width>m<mux>w<wmask>, for example
# sram22_256x32m4w8. We parse them out so the resolver can find the right
# directory without the caller having to know the BWRC layout.
SRAM22_NAME_RE = re.compile(r"^sram22_(\d+)x(\d+)m(\d+)w(\d+)$")


@dataclass
class DiscoveredModules:
    """What an RTL scan found: modules defined, modules used, files walked."""
    user_defined: Set[str]      # modules the user's RTL declares
    instantiated: Set[str]      # modules the user's RTL references
    rtl_files: List[Path]       # every .v / .sv / .vh, in sorted order

    @property
    def undefined_references(self) -> Set[str]:
        """Modules used but not declared. These are the ones that need stubs."""
        return self.instantiated - self.user_defined


# ----- RTL scanning -----

# Matches a module declaration: "module foo" or "module foo (..." or
# "module foo #(...)". The trailing character class is what tells us we hit
# a real declaration and not, say, "modules:" in a comment.
MODULE_DECL_RE = re.compile(r"^\s*module\s+(\w+)\s*[#(\s]", re.MULTILINE)

# Verilog and SystemVerilog keywords that look enough like module names to
# trip our instantiation regex. We match the regex, then drop anything that
# turns out to be one of these. The "assert property(...)" case is what
# motivated the SV expansion at the bottom of the set.
VERILOG_KEYWORDS = {
    "module", "endmodule", "input", "output", "inout", "wire", "reg",
    "logic", "assign", "always", "always_ff", "always_comb", "always_latch",
    "initial", "begin", "end", "if", "else",
    "case", "endcase", "for", "while", "function", "endfunction", "task",
    "endtask", "generate", "endgenerate", "parameter", "localparam",
    "integer", "genvar", "default", "posedge", "negedge", "and", "or", "not",
    "nand", "nor", "xor", "xnor", "buf", "supply0", "supply1", "tri",
    "return", "automatic", "static", "void", "real", "time", "string",
    "typedef", "struct", "union", "enum", "package", "endpackage",
    "import", "export", "extern", "virtual", "class", "endclass",
    # SystemVerilog assertion grammar: "assert property(...);" looks like
    # a module instantiation if you squint. Same for cover/assume/expect.
    "assert", "assume", "cover", "expect", "property", "endproperty",
    "sequence", "endsequence", "checker", "endchecker", "restrict",
    "disable", "bind",
    # System tasks like $display, $finish. The regex strips the $ so
    # they look like bare identifiers, hence ending up in this set.
    "display", "write", "monitor", "strobe", "finish", "stop",
    "fopen", "fclose", "fwrite", "readmemh", "readmemb",
}
# An instantiation looks like "<module_name> <instance_name>(". The
# instance name part is loose because generate blocks (sram_data[0].sram)
# and escaped identifiers (\foo.bar) make Verilog instance names messy.
# Anything we grab here gets cross-checked against VERILOG_KEYWORDS.
INSTANTIATION_RE = re.compile(
    r"^\s*(\w+)\s+(?:\\?\w+(?:\[[^\]]*\])?\.?\w*|\w+)\s*\(",
    re.MULTILINE,
)


# Things we drop on the floor by default when walking a directory. The list
# is what bit me on spring_asic_final_project: testbenches that aren't
# synthesizable, "Cache (copy).v" macOS duplicates that re-declare the same
# modules, etc. Override with --exclude on the CLI, or kill the default
# list entirely with --no-default-excludes.
DEFAULT_EXCLUDE_PATTERNS = [
    "*Testbench.v", "*Testbench.sv",
    "*TestBench.v", "*TestBench.sv",
    "*_tb.v", "*_tb.sv",
    "*_test.v",
    "* (copy).v",       # macOS Finder-style duplicates
    "* (*).v",          # anything with " (foo).v" suffix
    "test_*.v",
]


def _strip_ifdef_blocks(text: str) -> str:
    """
    Drop everything between `ifdef and `endif so dead code doesn't lie to us.

    Why bother: a file like Memory151.v might have an instantiation of
    no_cache_mem guarded by `ifdef no_cache_mem. Without that define set
    (and we have no way of knowing what defines will be set at compile
    time), the instantiation is dead code. If we don't strip it, the
    scanner reports no_cache_mem as a missing module and the user has
    to chase a phantom error.

    The conservative move is to assume no defines: drop every conditional
    branch and only look at the unconditional code. That can lose real
    instantiations if the design relies on a default-on `define, but in
    practice those are rare and the error message is clearer than the
    alternative.

    This is a dumb line-based scanner, not a real preprocessor. Nested
    `ifdefs work via depth counting. `define inside an `ifdef can confuse
    it, but I haven't hit that case yet.
    """
    out: List[str] = []
    depth = 0
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("`ifdef") or stripped.startswith("`ifndef"):
            depth += 1
            continue
        if stripped.startswith("`endif"):
            depth = max(0, depth - 1)
            continue
        if stripped.startswith("`else") or stripped.startswith("`elsif"):
            # The depth counter is already keeping us inside the block,
            # so we just skip the directive line itself.
            continue
        if depth == 0:
            out.append(line)
    return "\n".join(out)


def _is_excluded(path: Path, patterns: List[str]) -> bool:
    """
    Does this path match any of the user's exclude patterns?

    We try a couple of different matching strategies so the user can write
    either "basename" patterns like "*Testbench.v" or "subpath" patterns
    like "Cache/*.v" without having to think about which the tool expects.
    Pathlib's match() is anchored to the end of the path; fnmatch lets us
    do a substring-style match against the full path string.
    """
    import fnmatch
    name = path.name
    full = str(path)
    for pat in patterns:
        if Path(name).match(pat):
            return True
        try:
            if path.match(pat):
                return True
        except Exception:
            pass
        if fnmatch.fnmatch(full, pat) or fnmatch.fnmatch(full, f"*/{pat}"):
            return True
    return False


def scan_rtl_directory(
    rtl_paths: List[Path],
    exclude_patterns: Optional[List[str]] = None,
    use_default_excludes: bool = True,
) -> DiscoveredModules:
    """
    Walk the RTL paths and figure out which modules are defined vs used.

    Directory inputs get walked recursively but filtered through the
    default exclude list (testbenches, copies, etc.) plus anything the
    caller adds via exclude_patterns. File inputs are always included
    verbatim: if you hand the tool a specific .v file, we don't second
    guess you.

    The dedup step is the other thing worth knowing about: if two files
    declare the same module name, we keep the first one in sorted order
    and silently drop the others. That's how we handle "Cache.v" coexisting
    with "Cache/Cache (copy).v" or similar; the user shouldn't have to
    explicitly --exclude every duplicate.
    """
    user_defined: Set[str] = set()
    instantiated: Set[str] = set()
    rtl_files: List[Path] = []

    excludes: List[str] = []
    if use_default_excludes:
        excludes.extend(DEFAULT_EXCLUDE_PATTERNS)
    if exclude_patterns:
        excludes.extend(exclude_patterns)

    skipped: List[Path] = []
    for p in rtl_paths:
        if p.is_dir():
            for candidate in sorted(list(p.rglob("*.v")) + list(p.rglob("*.sv"))):
                if _is_excluded(candidate, excludes):
                    skipped.append(candidate)
                else:
                    rtl_files.append(candidate)
        elif p.is_file():
            # User passed a specific file; trust them and skip filtering.
            rtl_files.append(p)

    # First pass: dedup by module name. If two files declare the same
    # module, we keep the one that sorts first and drop the rest. The
    # canonical case this handles is "Cache.v" + "Cache/Cache (copy).v".
    seen_modules: Set[str] = set()
    deduplicated: List[Path] = []
    skipped_dup: List[Tuple[Path, Set[str]]] = []
    for f in rtl_files:
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        text = re.sub(r"//[^\n]*", "", text)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        text = _strip_ifdef_blocks(text)
        modules_in_file = {m.group(1) for m in MODULE_DECL_RE.finditer(text)}
        dup = modules_in_file & seen_modules
        if dup:
            skipped_dup.append((f, dup))
            continue
        seen_modules.update(modules_in_file)
        deduplicated.append(f)

    if skipped:
        print(f"  Excluded by patterns: {len(skipped)} files "
              f"(e.g. {[str(s.name) for s in skipped[:3]]})",
              file=sys.stderr)
    if skipped_dup:
        print(f"  Excluded as duplicates: {len(skipped_dup)} files "
              f"(e.g. {[str(s.name) for s, _ in skipped_dup[:3]]})",
              file=sys.stderr)

    rtl_files = deduplicated

    # Second pass: now that we know which files we're keeping, find all the
    # module declarations and instantiations across the final set. Comments
    # and `ifdef'd-out code get stripped so we don't pick up false hits.
    for f in rtl_files:
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        text = re.sub(r"//[^\n]*", "", text)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        text = _strip_ifdef_blocks(text)

        for m in MODULE_DECL_RE.finditer(text):
            user_defined.add(m.group(1))

        for m in INSTANTIATION_RE.finditer(text):
            candidate = m.group(1)
            # The regex is loose; the keyword set is how we filter the
            # false positives back out.
            if candidate in VERILOG_KEYWORDS:
                continue
            instantiated.add(candidate)

    return DiscoveredModules(
        user_defined=user_defined,
        instantiated=instantiated,
        rtl_files=rtl_files,
    )


# ----- SRAM22 metadata + path resolution -----

@dataclass
class Sram22Info:
    """Everything we need to know about one sram22 macro to wire it up."""
    name: str           # e.g. sram22_64x32m4w8
    depth: int          # number of words (64)
    width: int          # word width in bits (32)
    mux: int            # the m# part of the name (column mux factor)
    wmask: int          # the w# part of the name (write mask granularity)
    addr_width: int     # log2(depth), needed for the blackbox stub
    base_dir: Path      # macro's directory under BWRC_SRAM22_ROOT
    lef_file: Path
    lib_file: Path
    gds_file: Path
    verilog_sim: Path


def resolve_sram22(name: str, sram22_root: str = BWRC_SRAM22_ROOT) -> Optional[Sram22Info]:
    """
    Turn a module name like sram22_64x32m4w8 into the paths Hammer needs.

    Returns None if the name doesn't look like a sram22 macro or if any of
    the deliverables are missing on disk. The caller can use that signal
    to decide whether to fall back to a different resolver (OpenRAM, etc.)
    or to warn the user.
    """
    m = SRAM22_NAME_RE.match(name)
    if not m:
        return None
    depth, width, mux, wmask = (int(x) for x in m.groups())
    base = Path(sram22_root) / name
    if not base.is_dir():
        return None
    # Check that all four deliverables are present. If the macro was only
    # half-installed we'd rather fail here than emit a config that points
    # at a missing file and watch Innovus blow up later.
    lef = base / f"{name}.lef"
    gds = base / f"{name}.gds"
    vsim = base / f"{name}.v"
    # The .lib file is per-corner; tt_025C_1v80 is the only one we care
    # about for the typical sky130 flow. It may have a .rc / .rcc / .c
    # suffix depending on extraction fidelity; pick whichever exists.
    lib_candidates = sorted(base.glob(f"{name}_tt_025C_1v80*.lib"))
    if not (lef.is_file() and gds.is_file() and vsim.is_file() and lib_candidates):
        return None
    addr_width = (depth - 1).bit_length()
    return Sram22Info(
        name=name,
        depth=depth, width=width, mux=mux, wmask=wmask,
        addr_width=addr_width,
        base_dir=base,
        lef_file=lef,
        lib_file=lib_candidates[0],
        gds_file=gds,
        verilog_sim=vsim,
    )


# ----- Blackbox stub generation -----

def generate_blackbox_stub(info: Sram22Info) -> str:
    """
    Make a port-only Verilog stub Genus can elaborate without synthesizing.

    The vendor's .v file is a full behavioral model: it has a `reg` array
    for storage and `always` blocks for the read/write logic. If you hand
    that to Genus it'll compile it just like any other RTL, which means
    your nice 64x32 SRAM becomes 2048 flip-flops sprinkled into the
    surrounding cache module. Worse, the macro instance vanishes from
    the netlist hierarchy, so par has nothing to call place_inst on.

    What we want instead is a stub that declares only the port list and
    no body. Genus treats it as a hard macro (since LEF/lib show up via
    extra_libraries in sky130.yml), preserves the instance in the netlist,
    and lets par handle the physical side.
    """
    return f"""// AUTO-GENERATED blackbox stub for {info.name}
// DO NOT EDIT. Regenerate via `studio design-register`.
// Sourced from {info.verilog_sim}

module {info.name}(
`ifdef USE_POWER_PINS
    vdd,
    vss,
`endif
  clk, we, wmask, addr, din, dout
);

  parameter DATA_WIDTH  = {info.width};
  parameter ADDR_WIDTH  = {info.addr_width};
  parameter WMASK_WIDTH = {info.wmask};
  parameter RAM_DEPTH   = 1 << ADDR_WIDTH;

`ifdef USE_POWER_PINS
  inout vdd;
  inout vss;
`endif
  input                       clk;
  input                       we;
  input  [WMASK_WIDTH-1:0]    wmask;
  input  [ADDR_WIDTH-1:0]     addr;
  input  [DATA_WIDTH-1:0]     din;
  output reg [DATA_WIDTH-1:0] dout;

  // No body. par/Innovus uses the LEF/lib instead.
endmodule
"""


# ----- Config file emission -----

def emit_common_yml(
    design: str,
    top_module: str,
    rtl_files: List[Path],
    blackbox_stubs: List[Path],
) -> str:
    """Build the common.yml that lists the top module and all input files."""
    lines: List[str] = []
    lines.append(f"# AUTO-GENERATED by studio design-register for '{design}'.")
    lines.append("# Regenerate (will overwrite) by running design-register again.")
    lines.append("")
    lines.append("vlsi.core.build_system: make")
    lines.append("")
    lines.append('vlsi.inputs.power_spec_type: "cpf"')
    lines.append('vlsi.inputs.power_spec_mode: "auto"')
    lines.append("")
    lines.append("synthesis.inputs:")
    lines.append(f'  top_module: "{top_module}"')
    lines.append("  input_files:")
    for f in rtl_files:
        lines.append(f'    - "{f}"')
    if blackbox_stubs:
        lines.append("    # Blackbox stubs for the macros the design instantiates.")
        lines.append("    # Do NOT swap these for the vendor's behavioral .v files. Genus")
        lines.append("    # will treat the behavioral model as RTL, compile its `reg mem[...]`")
        lines.append("    # storage into flip-flops, and quietly destroy the macro hierarchy.")
        lines.append("    # par then can't place the SRAM because there's no SRAM instance left.")
        for s in blackbox_stubs:
            lines.append(f'    - "{s}"')
    lines.append("")
    lines.append("sim.inputs:")
    lines.append(f'  top_module: "{top_module}"')
    lines.append("")
    lines.append("power.inputs:")
    lines.append(f'  top_module: "{top_module}"')
    lines.append("")
    return "\n".join(lines)


def emit_syn_yml(design: str, clock_ns: float) -> str:
    """Build syn.yml with the clock spec and a starting-point input delay."""
    # A common rule of thumb is to budget about 20% of the clock period for
    # input arrival, which gives synthesis some margin. Designs that need
    # tighter or looser numbers can edit this after generation.
    fifth = clock_ns / 5.0
    return f"""# AUTO-GENERATED by studio design-register for '{design}'.
# Regenerate (will overwrite) by running design-register again.

vlsi.inputs.clocks:
  - {{name: "clk", period: "{clock_ns}ns", uncertainty: "0.1ns"}}

# Starting point: input arrival = clock_period / 5.
# Tighten or loosen per design once you have real timing data.
vlsi.inputs.delays:
  - {{name: "mem*", clock: "clk", direction: "input", delay: "{fifth:.2f}ns"}}
"""


def emit_sky130_yml(design: str, clock_ns: float, srams: List[Sram22Info]) -> str:
    """Build sky130.yml: stackup, sim defines, and one extra_libraries entry per macro."""
    lines: List[str] = []
    lines.append(f"# AUTO-GENERATED by studio design-register for '{design}'.")
    lines.append("# Regenerate (will overwrite) by running design-register again.")
    lines.append("")
    lines.append('technology.core.stackup: "sky130_fd_sc_hd"')
    lines.append('vlsi.technology.placement_site: "unithd"')
    lines.append("")
    lines.append("sim.inputs:")
    lines.append(f'  defines: ["CLOCK_PERIOD={clock_ns}"]')
    lines.append('  defines_meta: "append"')
    lines.append("")

    if srams:
        # This block is the key piece. Without it, Innovus has no LEF/lib
        # for the SRAM macros and place_inst fails with IMPTCM-162. The
        # sky130 SRAM generator in hammer/technology/sky130/sram_compiler/
        # has the logic to produce these entries, but the combined syn_par
        # action doesn't invoke it, so we just build them directly.
        lines.append("# SRAM macro library bindings, one per macro the design uses.")
        lines.append("# These feed each macro's LEF + .lib + GDS through Hammer's tech")
        lines.append("# plugin into Innovus, which is what lets par's place_inst find")
        lines.append("# the macros and the timing engine model them as cells.")
        lines.append("vlsi.technology.extra_libraries:")
        for s in srams:
            lines.append("  - library:")
            lines.append(f'      name: "{s.name}"')
            lines.append(f'      nldm_liberty_file: "{s.lib_file}"')
            lines.append(f'      lef_file: "{s.lef_file}"')
            lines.append(f'      gds_file: "{s.gds_file}"')
            lines.append(f'      verilog_sim: "{s.verilog_sim}"')
            lines.append('      corner: {nmos: typical, pmos: typical, temperature: "25 C"}')
            lines.append('      supplies: {VDD: "1.80 V", GND: "0 V"}')
            lines.append('      provides: [{lib_type: sram, vt: svt}]')
    lines.append("")
    return "\n".join(lines)


def emit_par_yml(
    design: str,
    top_module: str,
    srams: List[Sram22Info],
    fp_width: int = 1600,
    fp_height: int = 1600,
) -> str:
    """
    Build par.yml with the toplevel floorplan + a skeleton for macro placements.

    We can't auto-place the macros yet (that's the design-suggest-placements
    feature on the roadmap), so for every SRAM we just leave a commented-out
    TODO block with the right shape. The user needs to:

      1. Run syn once to see how the netlist hierarchy actually names the
         macro instances. Generate blocks turn into things like
         "sram_data[0].sram" or "data_srams[0].dsram" depending on the RTL.
      2. Uncomment one entry per instance and fill in x/y/orientation.
      3. Re-run par.
    """
    lines: List[str] = []
    lines.append(f"# AUTO-GENERATED by studio design-register for '{design}'.")
    lines.append("# Re-running design-register will overwrite this file. If you've added")
    lines.append("# custom placement constraints, copy them out first.")
    lines.append("")
    lines.append("vlsi.inputs.placement_constraints:")
    lines.append(f'  - path: "{top_module}"')
    lines.append("    type: toplevel")
    lines.append(f"    margins: {{left: 0, right: 0, top: 0, bottom: 0}}")
    lines.append("    x: 0")
    lines.append("    y: 0")
    lines.append(f"    width: {fp_width}")
    lines.append(f"    height: {fp_height}")
    if srams:
        lines.append("")
        lines.append("  # ==== MACRO PLACEMENTS — YOU NEED TO FILL THESE IN ====")
        lines.append("  # Every macro instance the design has must be placed somewhere,")
        lines.append("  # or routing will die with NRIG-91 'not placed on the manufacturing")
        lines.append("  # grid' once it hits the unplaced macro.")
        lines.append("  #")
        lines.append("  # The skeleton below has one entry per macro module, but the design")
        lines.append("  # probably has multiple instances of each (generate blocks etc.).")
        lines.append("  # Easiest workflow: run syn once, then grep the *.mapped.v for the")
        lines.append("  # macro module names to find every instance path, then add one")
        lines.append("  # placement entry per instance.")
        for s in srams:
            lines.append("")
            lines.append("  # TODO: copy this block per instance and fill in path + x + y.")
            lines.append(f'  # - path: "{top_module}/<instance_path>/{s.name}_inst"')
            lines.append("  #   type: hardmacro")
            lines.append("  #   x: 100")
            lines.append("  #   y: 100")
            lines.append('  #   orientation: "r0"')
            lines.append('  #   top_layer: "met3"')
    lines.append("")
    lines.append("vlsi.inputs.pin_mode: generated")
    lines.append("vlsi.inputs.pin.generate_mode: semi_auto")
    lines.append("vlsi.inputs.pin.assignments:")
    lines.append('  - {pins: "*", layers: ["met4"], side: "bottom"}')
    lines.append("")
    return "\n".join(lines)


# ----- Top-level orchestration -----

def register_design(
    name: str,
    top_module: str,
    clock_ns: float,
    rtl_paths: List[Path],
    pdk: str = "sky130",
    out_dir: Optional[Path] = None,
    sram22_root: str = BWRC_SRAM22_ROOT,
    exclude_patterns: Optional[List[str]] = None,
    use_default_excludes: bool = True,
) -> Tuple[Path, List[str]]:
    """
    Scan the RTL, work out the macros, and write all four configs to disk.

    Returns (output_dir, warnings) where warnings is a list of human-readable
    strings the caller should surface to the user, mostly about manual steps
    they still need to take (macro placements, undefined modules we don't
    recognize, etc.).
    """
    if pdk != "sky130":
        raise NotImplementedError(f"PDK '{pdk}' not supported yet; only sky130")

    if out_dir is None:
        # If we're running from a Hammer repo checkout, drop into the same
        # e2e/configs-design/ layout the rest of the project uses. Otherwise
        # fall back to a relative path; the caller can override with --out-dir.
        cwd = Path.cwd()
        if (cwd / "e2e").is_dir():
            out_dir = cwd / "e2e" / "configs-design" / name
        else:
            out_dir = Path("e2e/configs-design") / name

    out_dir.mkdir(parents=True, exist_ok=True)
    stubs_dir = out_dir / "sram_blackbox_stubs"
    stubs_dir.mkdir(parents=True, exist_ok=True)

    warnings: List[str] = []

    # Step 1: walk the RTL and figure out the module landscape.
    print(f"Scanning RTL under: {[str(p) for p in rtl_paths]}", file=sys.stderr)
    discovered = scan_rtl_directory(
        rtl_paths,
        exclude_patterns=exclude_patterns,
        use_default_excludes=use_default_excludes,
    )
    print(f"  Found {len(discovered.rtl_files)} RTL files", file=sys.stderr)
    print(f"  Defined modules: {len(discovered.user_defined)}", file=sys.stderr)
    print(f"  Undefined references: {sorted(discovered.undefined_references)}", file=sys.stderr)

    # Step 2: classify the undefined references. The ones that match the
    # sram22 naming pattern go to the resolver. Anything else gets a warning;
    # Genus will refuse to elaborate it unless the user does something about it.
    srams: List[Sram22Info] = []
    other_undefined: List[str] = []
    for mod in sorted(discovered.undefined_references):
        info = resolve_sram22(mod, sram22_root=sram22_root)
        if info is not None:
            srams.append(info)
        elif SRAM22_NAME_RE.match(mod):
            # Name parses as sram22 but the macro dir isn't here. Probably
            # a different cluster, or a typo, or a half-installed macro.
            warnings.append(
                f"Module '{mod}' looks like a sram22 macro but the directory "
                f"{Path(sram22_root) / mod} doesn't exist on this host."
            )
        else:
            other_undefined.append(mod)

    if other_undefined:
        warnings.append(
            f"Undefined module(s) that aren't recognized macro types: "
            f"{other_undefined}. Genus will fail at elaborate unless you "
            f"provide stubs for these or set hdl_error_on_blackbox=false."
        )

    print(f"  Resolved {len(srams)} sram22 macros: {[s.name for s in srams]}", file=sys.stderr)

    # Step 3: write a blackbox stub for each macro we resolved. These are
    # what Genus sees during syn (so it doesn't synthesize the macro's
    # storage into FFs).
    stub_paths: List[Path] = []
    for s in srams:
        stub_path = stubs_dir / f"{s.name}.v"
        stub_path.write_text(generate_blackbox_stub(s))
        stub_paths.append(stub_path)
        print(f"  Wrote stub: {stub_path}", file=sys.stderr)

    # Step 4: write the four config files.
    (out_dir / "common.yml").write_text(
        emit_common_yml(name, top_module, discovered.rtl_files, stub_paths)
    )
    (out_dir / "syn.yml").write_text(emit_syn_yml(name, clock_ns))
    (out_dir / "sky130.yml").write_text(emit_sky130_yml(name, clock_ns, srams))
    (out_dir / "par.yml").write_text(emit_par_yml(name, top_module, srams))

    print(f"\nWrote configs to: {out_dir}", file=sys.stderr)

    if srams:
        warnings.append(
            f"par.yml needs hand-edits: add placement constraints for the "
            f"{len(srams)} SRAM macro instance(s) before triggering par. "
            f"Use a syn run to find the actual hierarchical instance names "
            f"(grep '*.mapped.v' for the macro module names)."
        )

    return out_dir, warnings


def lint_par_yml(design_dir: Path) -> List[str]:
    """
    Scan par.yml for commented-out placement constraints that look like SRAM
    macros. These are the classic "user uncommented some but forgot others"
    bug pattern that leads to PAR's NRIG-91 (off-grid macro) routing abort.

    Returns a list of warning lines. Empty list if everything looks clean.
    """
    par_yml = design_dir / "par.yml"
    if not par_yml.exists():
        return []

    warnings: List[str] = []
    suspicious: List[Tuple[int, str]] = []
    pattern = re.compile(r'^\s*#\s*-\s*path:\s*["\']?.*sram', re.IGNORECASE)
    for lineno, raw in enumerate(par_yml.read_text().splitlines(), start=1):
        if pattern.match(raw):
            suspicious.append((lineno, raw.strip()))

    if suspicious:
        warnings.append(
            f"{par_yml.name} has {len(suspicious)} SRAM placement constraint(s) "
            f"still commented out. PAR routing will fail with NRIG-91 if these "
            f"macros are actually instantiated. Review or remove:"
        )
        for lineno, line in suspicious:
            warnings.append(f"    L{lineno}: {line}")

    return warnings


def _emit_sky130_extras_yml(srams: List[Sram22Info]) -> str:
    lines: List[str] = []
    lines.append("# AUTO-GENERATED by studio augment.")
    lines.append("# SRAM macro library bindings only. The user's other sky130")
    lines.append("# config (stackup, placement_site, etc.) stays in sky130.yml.")
    lines.append("# Include both files in your hammer-vlsi run.")
    lines.append("")
    if not srams:
        lines.append("# No SRAM macros detected. This file is intentionally empty.")
        return "\n".join(lines) + "\n"
    lines.append("vlsi.technology.extra_libraries:")
    for s in srams:
        lines.append("  - library:")
        lines.append(f'      name: "{s.name}"')
        lines.append(f'      nldm_liberty_file: "{s.lib_file}"')
        lines.append(f'      lef_file: "{s.lef_file}"')
        lines.append(f'      gds_file: "{s.gds_file}"')
        lines.append(f'      verilog_sim: "{s.verilog_sim}"')
        lines.append('      corner: {nmos: typical, pmos: typical, temperature: "25 C"}')
        lines.append('      supplies: {VDD: "1.80 V", GND: "0 V"}')
        lines.append('      provides: [{lib_type: sram, vt: svt}]')
    lines.append("")
    return "\n".join(lines)


def augment_existing_design(
    design_dir: Path,
    sram22_root: str = BWRC_SRAM22_ROOT,
) -> Tuple[List[Sram22Info], List[str]]:
    """
    Read configs-design/<name>/common.yml, detect SRAMs in the listed RTL,
    and write blackbox stubs + sky130-extras.yml. Leaves the user's existing
    common.yml / syn.yml / sky130.yml / par.yml untouched.
    """
    import yaml

    warnings: List[str] = []
    common_yml = design_dir / "common.yml"
    if not common_yml.is_file():
        raise FileNotFoundError(
            f"{common_yml} does not exist. augment expects the design's "
            f"common.yml to already be in place (with synthesis.inputs.input_files)."
        )

    with common_yml.open("r") as f:
        cfg = yaml.safe_load(f) or {}

    syn_in = (cfg.get("synthesis.inputs")
              or cfg.get("synthesis", {}).get("inputs", {}))
    input_files = syn_in.get("input_files", []) if isinstance(syn_in, dict) else []
    if not input_files:
        input_files = cfg.get("synthesis.inputs.input_files", []) or []

    if not input_files:
        raise ValueError(
            f"Couldn't find synthesis.inputs.input_files in {common_yml}. "
            f"Make sure the design's common.yml lists its RTL."
        )

    rtl_paths = [Path(p) for p in input_files]
    print(f"Scanning {len(rtl_paths)} RTL files listed in {common_yml.name}...",
          file=sys.stderr)
    discovered = scan_rtl_directory(rtl_paths, use_default_excludes=False)
    print(f"  Undefined references: {sorted(discovered.undefined_references)}",
          file=sys.stderr)

    srams: List[Sram22Info] = []
    for mod in sorted(discovered.undefined_references):
        info = resolve_sram22(mod, sram22_root=sram22_root)
        if info is not None:
            srams.append(info)
        elif SRAM22_NAME_RE.match(mod):
            warnings.append(
                f"'{mod}' looks like a sram22 macro but isn't installed at "
                f"{Path(sram22_root) / mod}."
            )

    print(f"  Resolved {len(srams)} sram22 macros: {[s.name for s in srams]}",
          file=sys.stderr)

    stubs_dir = design_dir / "sram_blackbox_stubs"
    stubs_dir.mkdir(parents=True, exist_ok=True)
    stub_paths: List[Path] = []
    for s in srams:
        p = stubs_dir / f"{s.name}.v"
        p.write_text(generate_blackbox_stub(s))
        stub_paths.append(p)
        print(f"  Wrote stub: {p}", file=sys.stderr)

    extras_path = design_dir / "sky130-extras.yml"
    extras_path.write_text(_emit_sky130_extras_yml(srams))
    print(f"  Wrote: {extras_path}", file=sys.stderr)

    if stub_paths:
        stubs_listed = any(
            str(stubs_dir) in str(Path(p)) or "sram_blackbox_stubs" in str(p)
            for p in input_files
        )
        if not stubs_listed:
            warnings.append(
                f"Blackbox stubs were written to {stubs_dir}, but common.yml's "
                f"synthesis.inputs.input_files doesn't list them. Add these to "
                f"that list so Genus reads them:\n    "
                + "\n    ".join(f'- "{p}"' for p in stub_paths)
            )

    if srams:
        warnings.append(
            f"Add 'sky130-extras.yml' to your hammer-vlsi -p list so par "
            f"picks up the SRAM LEF/lib bindings."
        )

    warnings.extend(lint_par_yml(design_dir))

    return srams, warnings
