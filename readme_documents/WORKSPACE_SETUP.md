# Setting up SledgeHammer in a workspace

Quick reference for dropping SledgeHammer into any Hammer workspace (gcd-vlsi,
a chipyard tree, etc.). Run from the workspace root.

```bash
# 1. Populate any tech/PDK plugins you want (they are git submodules; empty
#    until initialized). Skip the ones you do not need.
git submodule update --init <plugin-name>        # e.g. hammer-tstechn7-plugin

# 2. Put the SledgeHammer hammer fork in hammer/
rm -rf hammer
git clone git@github.com:lawrencejrhee/hammer.git hammer

# 3. Build it -- one command, no manual pip
./hammer/scripts/uv_setup.sh

# 4. Use it
source hammer/.venv/bin/activate
```

Notes:

- `uv_setup.sh` creates `hammer/.venv`, installs hammer + its deps, and
  auto-installs any `hammer-*-plugin` sibling that is populated (has a
  `pyproject.toml`). Empty/uninitialized submodules are skipped -- that is why
  step 1 comes before step 3.
- It sanitizes the build environment first (strips conda/mamba/venv/LD_PRELOAD),
  so it runs fine with `(base)` active and won't produce a conda-tainted
  psycopg2.
- Skip plugin installs entirely with `SLEDGE_NO_PLUGINS=1 ./hammer/scripts/uv_setup.sh`.
- The DRC/LVS legs need the private mentor (Calibre) plugin; syn/par do not.
