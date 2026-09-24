# Flux Capacitor

**Queue-based autoscaler — scale on queue depth with cold-start mitigation and graceful scale-down.**

The thesis: idle GPUs kill startups (money) and slow scale kills users (SLO). Both failures share one root: reactive controllers measure the wrong signal or act at the wrong time.

Part of the [TechGuardCoders portfolio](https://github.com/orgs/TechGuardCoders/repositories): Cost Peep → Batcher → Spinal → Ball Knowledge → Bastion → Furnace → Truffle → Popcorn → **Flux Capacitor** → …

## The controller's three disciplines

1. **Damped scale-up** — queue above `queue_high` for `stable_window` consecutive ticks before acting. One blip must not panic-scale (GPU cold starts cost real money).
2. **Pre-warm (cold-start mitigation)** — when queue load crosses 70% of online capacity, start ONE worker *before* the queue trigger fires. Predictive nudge beats reactive panic: the worker is mid-cold-start when the burst actually lands.
3. **Graceful scale-down** — calm streak required, and workers are marked *draining* (finish the queue) before dying — never killed with in-flight work.

## Burst drill (measured, `benchmarks/`)

3-phase profile: calm 2 req/s (5 ticks) → **3× burst** (20 ticks) → calm (15 ticks). Worker cold start: 8s (calibrated to real model-load latency).

| Metric | Controller (+pre-warm) | Naive reactive |
|---|---|---|
| Time-to-drain after burst | **15.0s** | 18.0s |
| Peak queue depth | **34** | 42 |
| Final workers (calm) | 7 → drains toward min | 7 → drains toward min |

**Pre-warm buys 3.0s faster drain and 8 fewer requests queued** at peak — that's the cold-start mitigation dividend, measured. Both end at the same steady-state count; the difference is entirely *when* workers start coming online.

## The plant is calibrated to our measured cluster

- 8s cold start ≈ GLM-5.3-Flash model load on the DGX Spark
- Worker rate 2 req/s ≈ per-stream capacity we measured in Batcher (per-request wall p50 ~2.9s at c=1... modeled as queue+service)
- The dynamics (queue formation, cold-start window, drain) are the real mechanics; the plant is a discrete-event simulation of them — no cloud, no cluster mutation, CI-safe.

## Run it

```bash
uv venv --python 3.11 .venv && uv pip install pytest
./.venv/Scripts/python flux_capacitor.py      # burst drill
./.venv/Scripts/python -m pytest tests/ -q
```

## Tests

6 offline tests: cold-start timing, queue formation, damping (streak discipline), graceful drain, pre-warm trigger, min-workers floor.

## What's next

Cost Peep integration: the controller's decisions (scale events, queue depth) render on the dashboard — autoscaler eyes and hands in one portfolio.
