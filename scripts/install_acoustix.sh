#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ACOUSTIX_DIR="${ACOUSTIX_ROOT:-/home/w/src/AcoustiX}"

echo "This installs TensorFlow, Mitsuba, and the modified Sionna tree and may download several GB."
echo "Target checkout: $ACOUSTIX_DIR"
read -r -p "Continue? [y/N] " answer
[[ "$answer" == "y" || "$answer" == "Y" ]] || exit 2

if [[ ! -d "$ACOUSTIX_DIR/.git" ]]; then
    mkdir -p "$(dirname -- "$ACOUSTIX_DIR")"
    git clone https://github.com/penn-waves-lab/AcoustiX.git "$ACOUSTIX_DIR"
fi
echo "Using AcoustiX commit $(git -C "$ACOUSTIX_DIR" rev-parse HEAD)"
if conda env list | awk '$1 == "odas-acoustix" { found=1 } END { exit !found }'; then
    conda env update -n odas-acoustix -f "$PROJECT_DIR/simulation/environment/acoustix.yml" --prune
else
    conda env create -f "$PROJECT_DIR/simulation/environment/acoustix.yml"
fi
conda run -n odas-acoustix python -m pip install "$ACOUSTIX_DIR/sionna" "mitsuba==3.5.2"
conda run -n odas-acoustix python -c '
import drjit, mitsuba, rapidfuzz, sionna, tensorflow as tf, yaml
gpus = tf.config.list_physical_devices("GPU")
mitsuba.set_variant("cuda_ad_rgb" if gpus else "llvm_ad_rgb")
print(f"TensorFlow {tf.__version__}; GPUs: {[gpu.name for gpu in gpus]}")
print(f"Sionna {sionna.__version__}; Mitsuba {mitsuba.__version__}; Dr.Jit {drjit.__version__}")
print(f"Mitsuba variant check: {mitsuba.variant()}")
print("AcoustiX dependencies OK")
'

echo "Set this before running demos:"
echo "export ACOUSTIX_ROOT=$ACOUSTIX_DIR"
