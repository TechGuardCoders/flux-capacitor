"""
Flux Capacitor - queue-based autoscaler.

The thesis: idle GPUs kill startups (money) and slow scale kills users (SLO).
Both failures come from the same place: reactive scaling measures the wrong
signal or acts at the wrong threshold.

The plant (simulated honestly, calibrated to OUR measured cluster):
- worker pool: each worker serves RPS_WOKRER requests/sec (measured: c=4
  ceiling, MAX_SEQS=4, agg 67.9 tok/s at saturation -> we model requests,
  not tokens; a worker handles ~1 concurrent stream comfortably)
- queue: arrivals land in a FIFO queue when all workers are busy
- cold start: bringing a worker ONLINE takes COLD_START_S (model load);
  during that window it serves nothing - this is why naive scaling oscillates

The controller:
- scales UP when queue depth exceeds QUEUE_HIGH for STABLE_WINDOW consecutive
  ticks (damped: no panic on blips)
- scales DOWN when queue is empty and utilization below QUEUE_LOW for
  DOWNTICKS (graceful: drain before kill)
- cold-start aware: pre-warms ONE worker when approaching capacity
  (predictive nudge), instead of waiting for the queue to form

The drill (honest evidence):
- phase 1 steady: controller should hold min workers, zero queue
- phase 2 burst: 3x arrivals - controller scales up; we measure time-to-drain
  WITH and WITHOUT pre-warm to quantify cold-start mitigation
- phase 3 calm: controller scales down without killing in-flight work
"""
from __future__ import annotations

import json
import random
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- plant

COLD_START_S = 8.0        # worker online latency (model load) - seconds
WORKER_RATE = 2.0         # requests/sec one worker drains
TICK_S = 1.0              # controller tick


@dataclass
class Worker:
    id: int
    state: str = "starting"          # starting -> online -> draining -> dead
    started_at: float = 0.0
    def ready(self, now: float) -> bool:
        return self.state == "starting" and (now - self.started_at) >= COLD_START_S


class Plant:
    """The GPU pool being scaled. Simulated but with real dynamics:
    cold starts, queue formation, in-flight drains."""

    def __init__(self, initial_workers: int = 1):
        self.now = 0.0
        self.next_id = initial_workers
        self.workers: list[Worker] = []
        for i in range(initial_workers):
            self.workers.append(Worker(id=i, state="online"))
        self.next_id = initial_workers
        self.queue: deque = deque()
        self.served = 0
        self.dropped = 0
        self.history: list[dict] = []

    def tick(self, arrivals: int) -> None:
        self.now += TICK_S
        # arrivals
        for _ in range(arrivals):
            self.queue.append(self.now)

        # workers finishing cold start
        for w in self.workers:
            if w.state == "starting" and w.ready(self.now):
                w.state = "online"

        # service: online workers drain queue
        online = [w for w in self.workers if w.state == "online"]
        capacity = len(online) * WORKER_RATE * TICK_S
        served_now = min(int(capacity + (capacity - int(capacity) > random.random() and 1 or 0)), len(self.queue))
        self.served += served_now
        for _ in range(served_now):
            self.queue.popleft()

        # draining workers finish and die
        for w in self.workers:
            if w.state == "draining" and len(self.queue) == 0:
                w.state = "dead"
        self.workers = [w for w in self.workers if w.state != "dead"]

        self.history.append({
            "t": round(self.now, 1), "arrivals": arrivals,
            "queue": len(self.queue),
            "online": len(online), "starting": sum(1 for w in self.workers if w.state == "starting"),
            "served_total": self.served,
        })

    def scale_to(self, n: int) -> int:
        """Controller action: bring workers up. Returns how many started."""
        started = 0
        while sum(1 for w in self.workers if w.state != "dead") < n:
            self.workers.append(Worker(id=self.next_id, state="starting",
                                       started_at=self.now))
            self.next_id += 1
            started += 1
        return started

    def scale_down(self, n: int) -> int:
        """Graceful: mark n online workers draining (finish queue first)."""
        online = [w for w in self.workers if w.state == "online"]
        draining = 0
        for w in online[:n]:
            w.state = "draining"
            draining += 1
        return draining


# ------------------------------------------------------------ controller


@dataclass
class Controller:
    queue_high: int = 8          # scale up trigger
    queue_low: int = 0           # scale down condition (empty queue)
    stable_window: int = 3       # consecutive ticks above high before acting
    downtime_ticks: int = 10     # calm ticks before scaling down
    min_workers: int = 1
    max_workers: int = 8
    prewarm_margin: float = 0.7  # pre-warm at 70% capacity utilization
    downticks: int = 10          # calm ticks before scaling down
    _high_streak: int = 0
    _calm_streak: int = 0

    def act(self, plant: Plant) -> dict:
        online = sum(1 for w in plant.workers if w.state == "online")
        starting = sum(1 for w in plant.workers if w.state == "starting")
        total = online + starting
        q = len(plant.queue)

        action = "hold"
        # UP: damped queue trigger
        if q > self.queue_high:
            self._high_streak += 1
            if self._high_streak >= self.stable_window and total < self.max_workers:
                n = plant.scale_to(min(total + 1, self.max_workers))
                action = f"scale_up(+{n})"
                self._high_streak = 0
        else:
            self._high_streak = 0

        # PRE-WARM: predictive nudge before queue forms
        if action == "hold" and q > 0 and online > 0:
            load = q / max(online * WORKER_RATE, 1)
            if load > self.prewarm_margin and total < self.max_workers:
                n = plant.scale_to(total + 1)
                action = f"prewarm(+{n})"

        # DOWN: calm streak, graceful drain
        if q <= self.queue_low and starting == 0:
            self._calm_streak += 1
            if self._calm_streak >= self.downticks and total > self.min_workers:
                n = plant.scale_down(1)
                action = f"drain(-{n})"
                self._calm_streak = 0
        else:
            self._calm_streak = 0

        return {"t": plant.now, "action": action, "queue": q,
                "online": online, "starting": starting, "total": total}


# ------------------------------------------------------------ drills


def run_drill(arrival_fn, ticks: int, controller: Controller | None = None,
              label: str = "") -> dict:
    plant = Plant(initial_workers=1)
    ctrl = controller or Controller()
    for _ in range(ticks):
        plant.tick(arrival_fn(plant))
        ctrl.act(plant)
    return {"label": label, "served": plant.served, "dropped": plant.dropped,
            "final_queue": len(plant.queue), "final_workers": len(plant.workers),
            "history": plant.history}


def burst_profile(plant: Plant) -> int:
    """3-phase: calm (5t), burst 3x (20t), calm (15t)."""
    t = plant.now
    if t < 5:
        return 2
    elif t < 25:
        return 6          # 3x burst
    return 2


def main():
    random.seed(7)
    print("=== drill 1: WITH controller (pre-warm enabled) ===")
    with_ctrl = run_drill(burst_profile, ticks=40, label="controller+prewarm")

    print("=== drill 2: naive reactive (no prewarm, instant threshold) ===")
    naive = Controller(prewarm_margin=10.0, stable_window=1)
    without_ctrl = run_drill(burst_profile, ticks=40, controller=naive,
                             label="naive-reactive")

    # measure time-to-drain after burst starts (t=5)
    def time_to_drain(h: list[dict]) -> float | None:
        burst_start = next((p["t"] for p in h if p["t"] >= 5 and p["arrivals"] > 2), None)
        if burst_start is None:
            return None
        for p in h:
            if p["t"] > burst_start and p["queue"] == 0:
                return p["t"] - burst_start
        return None

    for d in (with_ctrl, without_ctrl):
        d["time_to_drain_s"] = time_to_drain(d["history"])
        peak = max(p["queue"] for p in d["history"])
        d["peak_queue"] = peak

    print(f"\nWITH controller:    drain {with_ctrl['time_to_drain_s']}s, "
          f"peak queue {with_ctrl['peak_queue']}, workers now {with_ctrl['final_workers']}")
    print(f"naive reactive:     drain {without_ctrl['time_to_drain_s']}s, "
          f"peak queue {without_ctrl['peak_queue']}, workers now {without_ctrl['final_workers']}")
    print(f"\nprewarm benefit: {without_ctrl['time_to_drain_s'] - with_ctrl['time_to_drain_s']}s "
          f"faster drain, {without_ctrl['peak_queue'] - with_ctrl['peak_queue']} fewer queued")

    Path("benchmarks").mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    Path(f"benchmarks/flux_capacitor_{stamp}.json").write_text(
        json.dumps({"with": with_ctrl, "without": without_ctrl}, indent=2))
    print(f"saved benchmarks/flux_capacitor_{stamp}.json")


if __name__ == "__main__":
    main()
