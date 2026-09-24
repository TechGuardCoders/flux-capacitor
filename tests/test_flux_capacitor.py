"""Offline tests: plant dynamics + controller damping logic."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from flux_capacitor import Plant, Controller, Worker, COLD_START_S, TICK_S  # noqa: E402


def test_cold_start_takes_time():
    p = Plant(initial_workers=0)
    p.scale_to(1)
    assert p.workers[0].state == "starting"
    p.tick(0)
    assert p.workers[0].state == "starting", "must still be starting after 1 tick"
    ticks_needed = int(COLD_START_S / TICK_S)
    for _ in range(ticks_needed):
        p.tick(0)
    assert p.workers[0].state == "online", f"online after {ticks_needed} ticks"


def test_queue_forms_when_capacity_exceeded():
    p = Plant(initial_workers=1)  # 1 online worker, rate 2/s
    p.tick(arrivals=5)            # capacity 2 -> queue 3
    assert len(p.queue) == 3


def test_damped_scaling_waits_for_streak():
    p = Plant(initial_workers=1)
    c = Controller(stable_window=3, queue_high=2, prewarm_margin=10.0)  # prewarm off
    p.tick(arrivals=10)   # queue spikes
    assert c.act(p)["action"] == "hold", "blip must not panic-scale (streak 1/3)"
    p.tick(arrivals=10)
    assert c.act(p)["action"] == "hold", "streak 2/3 still holding"
    p.tick(arrivals=10)
    assert "scale_up" in c.act(p)["action"], "3rd consecutive tick above high -> act"


def test_graceful_scale_down_drains():
    p = Plant(initial_workers=2)
    c = Controller(downticks=2)
    p.tick(0); assert c.act(p)["action"] == "hold"    # streak 1
    p.tick(0); a = c.act(p)                            # streak 2 -> drain
    assert "drain" in a["action"]
    assert any(w.state == "draining" for w in p.workers), "marked draining, not killed"


def test_prewarm_triggers_below_queue_high():
    p = Plant(initial_workers=1)
    c = Controller(queue_high=100, prewarm_margin=0.5)  # queue trigger unreachable
    p.tick(arrivals=5)    # queue 3 vs capacity 2 -> load 1.5 > 0.5
    a = c.act(p)
    assert "prewarm" in a["action"], "approaching capacity -> predictive nudge"


def test_min_workers_never_scales_below():
    p = Plant(initial_workers=1)
    c = Controller(min_workers=1, downticks=1)
    for _ in range(6):
        p.tick(0); c.act(p)
    assert len(p.workers) >= 1, "never below min"
    assert sum(1 for w in p.workers if w.state == "online") >= 1
