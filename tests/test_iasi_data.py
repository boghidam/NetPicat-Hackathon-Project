"""The committed Iași dataset: real OpenStreetMap roads and CTP bus lines."""

import json

import pytest

from smartcity.data import DATA_DIR, load_iasi


def test_dataset_is_the_central_iasi_spine(iasi_net):
    net = iasi_net
    assert 250 <= len(net.nodes) <= 600 and 500 <= len(net.segments) <= 1200
    assert sum(n.signalised for n in net.nodes) >= 50
    assert 47.14 < net.proj.lat0 < 47.19 and 27.55 < net.proj.lon0 < 27.61  # really Iași
    assert "OpenStreetMap" in net.source
    names = {s.name for s in net.segments}
    for street in ("Bd. Independenței", "Bd. Carol I", "Bd. Tudor Vladimirescu", "Str. Păcurari"):
        assert street in names, street
    assert {name for name, _, _ in net.landmarks_ll} >= {"Copou", "Piața Unirii", "Podu Roș", "Păcurari"}


def test_edges_have_real_lengths_and_speed_limits(iasi_net):
    for seg in iasi_net.segments:
        assert 40.0 <= seg.length <= 1300.0
        assert 20 / 3.6 <= seg.speed_limit <= 70 / 3.6
        assert seg.cum[-1] == pytest.approx(seg.length)
        assert 0 < seg.stop_pos < seg.length
    kph = {round(s.speed_limit * 3.6) for s in iasi_net.segments}
    assert 50 in kph and len(kph) > 2  # urban default plus other posted limits


def test_requested_ctp_lines_are_present_and_contiguous(iasi_net):
    refs = {r.ref for r in iasi_net.routes}
    assert {"19", "28", "36", "41"} <= refs
    for route in iasi_net.routes:
        km = sum(iasi_net.segments[e].length for e in route.edges) / 1000
        assert 3.0 < km < 30.0, route.name
        for a, b in zip(route.edges, route.edges[1:] + route.edges[:1], strict=True):
            assert iasi_net.segments[a].to_node == iasi_net.segments[b].from_node


def test_routes_come_from_osm_relations_and_run_along_their_streets():
    routes = json.loads((DATA_DIR / "iasi_routes.json").read_text())["routes"]
    by_ref = {r["ref"]: r for r in routes}
    assert all(r["osm_relations"] for r in routes)
    graph, lines = load_iasi()
    names = {e.id: e.name for e in graph.edges}
    on = {n for e in next(r for r in lines if r.ref == "28").edges if (n := names[e])}
    assert "Bd. Independenței" in on  # line 28 runs along the boulevard
    assert by_ref["28"]["from"] and by_ref["28"]["to"]


def test_gates_exist_and_are_mutually_reachable(iasi_net):
    net = iasi_net
    assert 6 <= len(net.gates) <= net.cfg.max_gates
    for a in net.gates:
        for b in net.gates:
            if a != b:
                path = net.path_between(a, b)
                assert path, f"no way from gate {a} to gate {b}"
                assert net.segments[path[0]].from_node == a and net.segments[path[-1]].to_node == b


def test_every_signalised_junction_has_valid_phases(iasi_net):
    net = iasi_net
    for node in net.nodes:
        if not node.signalised:
            continue
        groups = net.phase_groups(node.id)
        flat = [a for g in groups for a in g]
        assert sorted(flat) == sorted(net.in_segs[node.id])  # every approach in exactly one phase
        assert all(1 <= len(g) <= 2 for g in groups)
        assert len(groups) >= 2  # never one permanent green
        for g in groups:
            if len(g) == 2:
                assert net.partner[g[0]] == g[1]


def test_geometry_is_continuous_across_each_edge(iasi_net):
    net = iasi_net
    for seg in net.segments[::7]:
        x0, y0, _, _ = net.point_at(seg.id, 0.0)
        x1, y1, _, _ = net.point_at(seg.id, seg.length)
        a, b = net.nodes[seg.from_node], net.nodes[seg.to_node]
        assert abs(x0 - a.x) < 30 and abs(y0 - a.y) < 30  # consolidated clusters may shift the end a little
        assert abs(x1 - b.x) < 30 and abs(y1 - b.y) < 30
