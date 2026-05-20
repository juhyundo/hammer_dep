#!/bin/bash

# 1. Define the Workspace dynamically
# This gets the absolute path of the directory where this script is located
export autoTA_WS="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "Detected AutoTA Workspace at: ${autoTA_WS}"

# 2. Move to that directory (just in case)
cd "${autoTA_WS}"

# 3. Download Miniforge (Conda)
echo "Downloading Miniforge..."
wget -O Miniforge3.sh "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh"

# 4. Install Miniforge silently (-b) to the 'conda' subdirectory
# -b = Batch mode (no manual "yes" needed)
# -p = Installation path
echo "Installing Conda to ${autoTA_WS}/conda..."
bash Miniforge3.sh -b -p "${autoTA_WS}/conda"

# 5. Initialize Conda for this script session only
eval "$("${autoTA_WS}/conda/bin/conda" shell.bash hook)"
conda activate

# 6. Install Python Packages
echo "Installing Python dependencies..."
pip install google-genai rich pyyaml

# 7. Cleanup (Optional: remove the installer to save space)
rm Miniforge3.sh

echo "✅ Installation Complete."