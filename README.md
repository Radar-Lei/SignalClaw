# SignalClaw

**LLM-Guided Evolutionary Synthesis of Interpretable Traffic Signal Control Skills**

[中文说明](README_zh.md)

## Overview

Traffic signal control in the real world requires strategies that are both **effective** and **interpretable**. This is where many existing approaches break down:

- deep reinforcement learning can optimize performance, but the learned policy is usually opaque and difficult to audit
- classical program synthesis is interpretable, but often constrained by a narrow hand-crafted DSL
- most existing methods are effectively **event-blind**, despite emergency vehicles, transit priority, incidents, and severe congestion being central to real deployment

SignalClaw addresses this gap by using large language models as **offline evolutionary skill generators** and deploying only compact executable traffic-control code inside SUMO.

Instead of shipping a black-box neural policy, SignalClaw evolves **self-documenting skills** that can be:

- inspected by traffic engineers
- audited and versioned
- modified without retraining a neural controller
- specialized for different event contexts and composed at runtime

Each skill contains:

- a strategy description
- a selection guidance block
- executable scoring code

## Why SignalClaw

SignalClaw is not just "LLM writes code for TSC". Its main advantages, as established in the paper, are:

- **Signal-driven evolution**: queue, delay, throughput, and stagnation statistics are distilled into structured evolution signals, then translated into natural-language feedback for the next generation.
- **Self-documenting policy artifacts**: the evolved unit is not a bare snippet but a structured skill with rationale, decision guidance, and executable code.
- **Event-driven compositional control**: emergency, incident, transit, congestion, and normal skills are evolved separately and composed by a deterministic priority dispatcher.
- **Deployment-ready determinism**: the LLM is used offline during evolution only; the deployed controller is plain code with predictable runtime behavior.
- **Zero-shot mixed-event composition**: independently evolved event skills can be composed on mixed scenarios without retraining.

## Repository Scope

This public repository currently includes:

- core framework code under `evoprog/`
- representative experiment scripts under `scripts/`
- original figure sources and rendered figures under `images/`
- original paper tables under `tables/`

This repository intentionally does **not** include:

- the paper manuscript PDF
- large simulation outputs
- full SUMO scenario assets and bulky training artifacts

## Framework

<p align="center">
  <a href="images/fig_framework.svg">
    <img src="images/fig_framework.svg" alt="SignalClaw framework" width="100%">
  </a>
</p>

The framework can be read in three layers:

- **Skill representation**: each evolved skill combines human-readable rationale, decision guidance, and executable code.
- **Evolution loop**: `Generate -> Test -> Evolve -> Solidify`.
- **Feedback mechanism**: simulation outcomes are converted into signals such as high queue, high delay, low throughput, and stagnation, which steer the next LLM mutation step.

This design is what gives SignalClaw its main interpretability advantage over RL and its structural flexibility advantage over fixed DSL search.

## Scenarios

<p align="center">
  <a href="images/fig_scenarios.svg">
    <img src="images/fig_scenarios.svg" alt="SignalClaw scenarios" width="100%">
  </a>
</p>

The evaluation covers twelve SUMO configurations on a `4x4` arterial grid:

- **Routine training**: `T1`, `T2`, `T3`
- **Routine validation**: `V1`, `V2`, `V3`, created with demand perturbations
- **Emergency**: `E1`, `E2`, testing ambulance preemption under different frequencies
- **Transit**: `B1`, `B2`, testing bus-priority behavior under passenger-weighted delay
- **Incident**: `I1`, testing response to a blocked lane
- **Mixed event**: `M1`, testing zero-shot composition under emergency plus incident

This scenario design is important: SignalClaw is evaluated not only on routine traffic, but also on sparse and safety-critical event conditions where event-blind policies usually fail.

## Key Results

The paper supports four main empirical claims:

- On routine traffic, SignalClaw stays within `3% to 10%` of the best method per scenario while maintaining low variance across seeds.
- On emergency scenarios, SignalClaw achieves the **lowest emergency delay** among all compared methods.
- On transit scenarios, SignalClaw achieves the **lowest person-delay**, which better reflects passenger-level social cost than vehicle-level delay.
- On mixed scenarios, independently evolved event skills compose correctly through the priority dispatcher without retraining.

### Evolution Improvement

| Skill | Scenarios | Initial | Best | Gen | Improvement |
|---|---|---:|---:|---:|---:|
| Normal | T1+T2+T3 | 55.20 | 60.50 | 12 | 9.6% |
| Emergency | E1+E2 | 3.15 | 3.92 | 22 | 24.4% |
| Transit | B1+B2 | 2.88 | 3.76 | 8 | 30.6% |
| Incident | I1 | 2.65 | 3.28 | 18 | 23.8% |

### Evolution Curves

<p align="center">
  <img src="images/fig_evolution_curves.png" alt="Evolution curves" width="860">
</p>

Across 30 generations, the normal skill improves from `55.20` to `60.50` fitness, while the event-specialized skills improve by `23.8% to 30.6%` depending on scenario type.

### Routine Traffic Performance

On the six routine scenarios, SignalClaw reaches average delay in the `7.8s to 9.2s` range and remains competitive with PI-Light and DQN while preserving full interpretability.

| Scenario | Type | FixedTime | MaxPressure | PI-Light | DQN | SignalClaw |
|---|---|---:|---:|---:|---:|---:|
| T1 | Train | 47.3 ± 1.5 | 13.8 ± 0.9 | 8.5 ± 0.7 | **7.9 ± 1.2** | 8.7 ± 0.6 |
| T2 | Train | 43.6 ± 1.3 | 12.5 ± 0.8 | 8.1 ± 0.6 | 8.4 ± 1.1 | **7.8 ± 0.4** |
| T3 | Train | 52.1 ± 1.8 | 14.2 ± 1.0 | **7.9 ± 0.8** | 8.3 ± 1.3 | 8.4 ± 0.7 |
| V1 | Valid | 49.8 ± 1.6 | 14.5 ± 1.0 | 9.3 ± 0.8 | **8.7 ± 1.4** | 9.1 ± 0.6 |
| V2 | Valid | 46.2 ± 1.4 | 13.9 ± 0.9 | 9.6 ± 0.7 | **8.5 ± 1.2** | 9.2 ± 0.8 |
| V3 | Valid | 51.5 ± 1.7 | 14.8 ± 1.1 | **8.8 ± 0.7** | 9.4 ± 1.5 | 9.1 ± 0.5 |

### Event-Aware Evaluation

The event results should be interpreted as an **end-to-end system comparison**: SignalClaw uses event-aware detector-dispatch control, while the baselines are event-blind and transferred from routine traffic.

The strongest outcomes are:

- **Emergency**: `11.2s to 18.5s` emergency delay for SignalClaw, versus `42.3s to 72.3s` for MaxPressure and `78.5s to 95.3s` for DQN.
- **Transit**: `9.8s to 11.5s` person-delay for SignalClaw, versus `38.7s to 45.2s` for MaxPressure.
- **Mixed event**: best emergency delay on `M1` while maintaining stable overall traffic delay.

| Scenario | Method | Avg Delay | Emergency Delay | Person-Delay | Queue |
|---|---|---:|---:|---:|---:|
| E1 | FixedTime | 48.5 ± 1.6 | 385.2 ± 48.7 | - | 32.4 ± 1.2 |
| E1 | MaxPressure | 14.2 ± 1.0 | 42.3 ± 6.8 | - | 8.9 ± 0.7 |
| E1 | PI-Light | **9.8 ± 0.8** | 55.2 ± 9.1 | - | 5.7 ± 0.5 |
| E1 | DQN | 11.3 ± 2.1 | 78.5 ± 32.4 | - | 6.8 ± 1.5 |
| E1 | SignalClaw | 11.5 ± 0.7 | **14.7 ± 2.8** | - | 6.9 ± 0.5 |
| E2 | FixedTime | 50.2 ± 1.8 | 425.8 ± 55.3 | - | 34.1 ± 1.3 |
| E2 | MaxPressure | 15.1 ± 1.1 | 72.3 ± 5.8 | - | 9.5 ± 0.8 |
| E2 | PI-Light | **10.5 ± 0.9** | 78.5 ± 7.6 | - | 6.2 ± 0.6 |
| E2 | DQN | 12.1 ± 2.3 | 95.3 ± 38.7 | - | 7.4 ± 1.6 |
| E2 | SignalClaw | 12.3 ± 0.7 | **11.2 ± 2.1** | - | 7.5 ± 0.5 |
| B1 | FixedTime | 47.8 ± 1.5 | - | 520.3 ± 68.5 | 31.9 ± 1.1 |
| B1 | MaxPressure | 13.5 ± 0.9 | - | 38.7 ± 5.2 | 8.3 ± 0.6 |
| B1 | PI-Light | **9.5 ± 0.7** | - | 42.3 ± 6.1 | 5.4 ± 0.4 |
| B1 | DQN | 10.8 ± 2.0 | - | 65.4 ± 28.6 | 6.5 ± 1.4 |
| B1 | SignalClaw | 10.9 ± 0.6 | - | **9.8 ± 1.5** | 6.6 ± 0.4 |
| B2 | FixedTime | 51.3 ± 1.9 | - | 485.6 ± 62.1 | 35.2 ± 1.4 |
| B2 | MaxPressure | 14.8 ± 1.1 | - | 45.2 ± 6.4 | 9.2 ± 0.7 |
| B2 | PI-Light | 10.8 ± 0.9 | - | 48.7 ± 7.2 | 6.4 ± 0.5 |
| B2 | DQN | **10.5 ± 2.2** | - | 58.3 ± 24.5 | 6.3 ± 1.5 |
| B2 | SignalClaw | 11.8 ± 0.8 | - | **11.5 ± 1.8** | 7.1 ± 0.6 |
| I1 | FixedTime | 53.2 ± 2.1 | - | - | 36.5 ± 1.5 |
| I1 | MaxPressure | 16.2 ± 1.3 | - | - | 10.3 ± 0.9 |
| I1 | PI-Light | 11.5 ± 0.9 | - | - | 6.9 ± 0.6 |
| I1 | DQN | 12.8 ± 2.5 | - | - | 7.9 ± 1.7 |
| I1 | SignalClaw | **10.8 ± 0.9** | - | - | **6.5 ± 0.6** |
| M1 | FixedTime | 54.1 ± 2.2 | 352.5 ± 45.8 | - | 37.2 ± 1.6 |
| M1 | MaxPressure | 16.8 ± 1.4 | 55.3 ± 8.1 | - | 10.7 ± 0.9 |
| M1 | PI-Light | **12.1 ± 1.0** | 62.4 ± 9.5 | - | 7.3 ± 0.6 |
| M1 | DQN | 13.5 ± 2.6 | 82.7 ± 35.4 | - | 8.3 ± 1.8 |
| M1 | SignalClaw | 13.2 ± 0.7 | **18.5 ± 3.2** | - | 8.0 ± 0.5 |

## Code Organization

```text
SignalClaw/
├── README.md
├── README_zh.md
├── main.py
├── pyproject.toml
├── requirements.txt
├── evoprog/
├── scripts/
│   ├── run_eventclaw_experiment.py
│   └── glm5_configs/
├── figures/
├── images/
│   ├── fig_framework.png
│   ├── fig_framework.svg
│   ├── fig_scenarios.png
│   ├── fig_scenarios.svg
│   ├── fig_evolution_curves.png
│   └── src/
├── scenarios/
│   └── README.md
└── tables/
    ├── TABLE_main_results.tex
    ├── TABLE_routine_results.tex
    └── TABLE_event_results.tex
```

Key directories:

- `evoprog/`: core evolution, evaluator, executor, LLM, and storage modules
- `scripts/`: experiment and configuration entry points
- `images/src/`: original paper figure sources
- `scenarios/`: placeholder location for SUMO scenario assets
- `tables/`: original paper table sources

## Quick Start

```bash
git clone <repository-url>
cd SignalClaw
pip install -e .
python main.py --help
```

If you want to run the full SUMO pipeline, you will also need:

- SUMO installed locally
- scenario assets placed under the expected `scenarios/` paths
- an OpenAI-compatible API endpoint or local LLM service

## Example Snippets

### Event Priority

```python
EVENT_PRIORITY = {
    "emergency": 0,
    "incident": 1,
    "transit": 2,
    "congestion": 3,
    "normal": 4,
}
```

### Normal Evolution Config

```toml
[evolution]
pop_size = 8
generations = 30
stagnation_threshold = 8
elite_count = 2

[store]
store_dir = "store/gpt5_evolve/normal"
```

### Interpretable Skill Example

```python
value[0] += (
    inlane_2_num_waiting_vehicle
    * max(1, inlane_2_num_vehicle)
    / max(1, inlane_2_vehicle_dist)
)

if outlane_2_num_vehicle > 5:
    value[0] -= outlane_2_num_vehicle ** 1.1
```

## License

`CC BY-NC 4.0`
