## 0A. First-time only: Environment setup
```bash
bash first-time.sh
```

## 0B. First-time only: Write API Key and Model Name in config.yml

## 1. Running the AutoTA

The AutoTA is designed to be run 1. in its own terminal 2. **from inside your lab folder** (e.g., `lab3`, `lab4`). It will automatically detect the configuration for that specific lab.

```
asic-labs-fa25-isabelleh123/
├── autoTA/
│   ├── config.yml
│   ├── extract.py
│   ├── gemini.py
│   └── README.md
├── lab1/
├── lab2/
├── lab3/
│   ├── build/
│   ├── src/
│   ├── design.yml
│   └── !! RUN 'python3 ../autoTA/gemini.py' HERE !!
└── ...
```

### Option 1: Default (Most Recent Run)
Use this command to automatically find and analyze the **most recently modified** log file in your `build/syn-rundir/` folder. This is the standard way to check your current work.

```bash
cd lab3
python3 ../autoTA/gemini.py
```

### Option 2: Analyze a Specific Log File
If you want to check a specific run (e.g., to compare against a previous attempt), you can provide the log filename as an argument. The script will look for this file inside `build/syn-rundir/`.

```bash
cd lab3
python3 ../autoTA/gemini.py genus.log12
```

## Troubleshooting

* **"File not found":** Ensure you are running the command from *inside* the lab folder (e.g., `lab3/`), not from the root directory.
* **"API Error":** Verify your API key in `autoTA/config.yml` is correct and you have internet access.
* **"No synthesis issues found":** This usually means the extraction script didn't find any fatal errors or mapped warnings in the log file. Check if your synthesis actually failed or if `make synth` ran successfully.


# Envrionment Setup (happens automatically when running autoTA)

Usually no need to do manually. Just here for reference.

```bash
# 1. Set your workspace variable
export GEMINI_CLI_WS=~/asic-labs-fa25-isabelleh123

# 2. Navigate to the workspace
cd ${GEMINI_CLI_WS}

# 3. Activate the Conda environment
eval "$(${GEMINI_CLI_WS}/conda/bin/conda shell.bash hook)"
conda activate
```