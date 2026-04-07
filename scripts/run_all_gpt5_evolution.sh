#!/bin/bash
# Run ALL skill evolution with GPT-5.4-high.
# 1. Normal (T1+T2+T3) → produces best normal skill
# 2. Emergency (E1+E2), Incident (I1), Transit (B1+B2), Mixed (M1)
#
# Usage:
#   bash scripts/run_all_gpt5_evolution.sh              # run all
#   bash scripts/run_all_gpt5_evolution.sh normal        # run normal only
#   bash scripts/run_all_gpt5_evolution.sh events        # run events only
#   bash scripts/run_all_gpt5_evolution.sh emergency     # run one type

set -e
cd "$(dirname "$0")/.."

# Load .env
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

run_evolve() {
    local config=$1
    local name=$2
    echo ""
    echo "============================================================"
    echo " Evolving: $name"
    echo " Config: $config"
    echo " Start: $(date)"
    echo "============================================================"
    python -m evoprog.daemon --config "$config"
    echo " Done: $(date)"
}

TARGET="${1:-all}"

# Step 1: Normal traffic evolution
if [ "$TARGET" = "all" ] || [ "$TARGET" = "normal" ]; then
    run_evolve scripts/glm5_configs/evolve_normal_t1t2t3.toml "Normal (T1+T2+T3)"
    echo ""
    echo "Normal evolution complete. Updating event configs with evolved normal skill..."
    # TODO: auto-update fixed_skills.normal in event configs with evolved code
fi

# Step 2: Event skill evolution (can run independently if normal is already done)
if [ "$TARGET" = "all" ] || [ "$TARGET" = "events" ] || [ "$TARGET" = "emergency" ]; then
    run_evolve scripts/glm5_configs/evolve_emergency_gpt5.toml "Emergency (E1+E2)"
fi

if [ "$TARGET" = "all" ] || [ "$TARGET" = "events" ] || [ "$TARGET" = "incident" ]; then
    run_evolve scripts/glm5_configs/evolve_incident_gpt5.toml "Incident (I1)"
fi

if [ "$TARGET" = "all" ] || [ "$TARGET" = "events" ] || [ "$TARGET" = "transit" ]; then
    run_evolve scripts/glm5_configs/evolve_transit_gpt5.toml "Transit (B1+B2)"
fi

if [ "$TARGET" = "all" ] || [ "$TARGET" = "events" ] || [ "$TARGET" = "mixed" ]; then
    run_evolve scripts/glm5_configs/evolve_mixed_gpt5.toml "Mixed (M1)"
fi

echo ""
echo "============================================================"
echo " All evolution complete!"
echo " Results in store/gpt5_evolve/"
echo "============================================================"
