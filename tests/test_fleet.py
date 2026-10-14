"""Fleet tests: per-node scaling decisions. Pure logic, no network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from flux_capacitor import (  # noqa: E402
    Controller, FleetController, NodeLoad, Plant, hottest_node)


def test_hottest_node_is_max_load():
    loads = [NodeLoad("a", queue=2, online=2),   # load 1.0
             NodeLoad("b", queue=5, online=1),   # load 5.0
             NodeLoad("c", queue=0, online=4)]   # load 0.0
    assert hottest_node(loads).name == "b"


def test_hottest_normalizes_by_capacity():
    """Bigger queue on a big node loses to a small queue on a starved node."""
    loads = [NodeLoad("big", queue=10, online=8),    # 1.25
             NodeLoad("small", queue=3, online=1)]   # 3.0
    assert hottest_node(loads).name == "small"


def test_hottest_tie_breaks_first():
    loads = [NodeLoad("first", queue=2, online=1), NodeLoad("second", queue=2, online=1)]
    assert hottest_node(loads).name == "first"


def test_zero_capacity_guard():
    """No online workers -> capacity floors at 1.0, no ZeroDivision."""
    n = NodeLoad("cold", queue=4, online=0)
    assert n.capacity == 1.0 and n.load == 4.0


def test_fleet_scales_only_hottest_node():
    """Burst on node b; node a stays untouched."""
    pa, pb = Plant(initial_workers=1), Plant(initial_workers=1)
    fc = FleetController(["a", "b"], stable_window=1, queue_high=2,
                         prewarm_margin=10.0)
    pb.tick(arrivals=10)   # b is hot
    pa.tick(arrivals=0)
    out = fc.act({"a": pa, "b": pb})
    assert out["node"] == "b"
    assert "scale_up" in out["action"] or "prewarm" in out["action"]
    assert len(pa.workers) == 1, "cold node must not be scaled"


def test_fleet_damping_is_per_node():
    """Streak state lives on each node's own controller."""
    pa, pb = Plant(initial_workers=1), Plant(initial_workers=1)
    fc = FleetController(["a", "b"], stable_window=3, queue_high=2,
                         prewarm_margin=10.0)
    pb.tick(arrivals=10)
    pa.tick(arrivals=0)
    assert fc.act({"a": pa, "b": pb})["action"] == "hold"   # b streak 1/3
    pb.tick(arrivals=10)
    assert fc.act({"a": pa, "b": pb})["action"] == "hold"   # streak 2/3
    pb.tick(arrivals=10)
    out = fc.act({"a": pa, "b": pb})
    assert "scale_up" in out["action"] and out["node"] == "b"


def test_fleet_single_node_matches_controller():
    """One-node fleet behaves like the plain Controller (default preserved)."""
    p1, p2 = Plant(initial_workers=1), Plant(initial_workers=1)
    fc = FleetController(["only"], stable_window=1, queue_high=2, prewarm_margin=10.0)
    solo = Controller(stable_window=1, queue_high=2, prewarm_margin=10.0)
    for _ in range(3):
        p1.tick(arrivals=10); p2.tick(arrivals=10)
        a = fc.act({"only": p1})
        b = solo.act(p2)
        assert a["action"] == b["action"]
