"""The committed Iași dataset: real OpenStreetMap roads and CTP bus lines."""

import json

import pytest

from smartcity.data import DATA_DIR, load_iasi

BACKBONE = ["20", "23", "28", "30", "36", "41", "42", "43", "47", "52"]


def test_dataset_covers_the_whole_city(iasi_net):
    net = iasi_net
    assert 200 <= len(net.nodes) <= 800 and 500 <= len(net.segments) <= 1500
    assert sum(n.signalised for n in net.nodes) >= 100
    assert 47.12 < net.proj.lat0 < 47.19 and 27.52 < net.proj.lon0 < 27.62  # really Iași
    assert net.width >= 12_000 and net.height >= 10_000  # city-wide, not just the centre
    assert "OpenStreetMap" in net.source
    names = {s.name for s in net.segments}
    for street in ("Bd. Independenței", "Bd. Carol I", "Bd. Tudor Vladimirescu", "Str. Păcurari"):
        assert street in names, street
    assert {name for name, _, _ in net.landmarks_ll} >= {"Copou", "Piața Unirii", "Podu Roș", "Păcurari"}


def test_edges_have_real_lengths_and_speed_limits(iasi_net):
    for seg in iasi_net.segments:
        assert 40.0 <= seg.length <= 7000.0  # the outskirts have long stretches between junctions
        assert 20 / 3.6 <= seg.speed_limit <= 70 / 3.6
        assert seg.cum[-1] == pytest.approx(seg.length)
        assert 0 < seg.stop_pos < seg.length
    kph = {round(s.speed_limit * 3.6) for s in iasi_net.segments}
    assert 50 in kph and len(kph) > 2  # urban default plus other posted limits


def test_the_city_has_16_to_20_ctp_lines_including_every_backbone_line(iasi_net):
    refs = [r.ref for r in iasi_net.routes]
    assert 16 <= len(refs) <= 20 and len(set(refs)) == len(refs)
    assert set(BACKBONE) <= set(refs)  # 20, 23, 28, 30, 36, 41, 42, 43, 47 and 52 are all there
    assert {r.ref for r in iasi_net.routes if r.backbone} == set(BACKBONE)  # and only they are flagged as backbone
    for route in iasi_net.routes:
        km = sum(iasi_net.segments[e].length for e in route.edges) / 1000
        assert 8.0 < km < 40.0, route.name  # a full loop: out and back
        for a, b in zip(route.edges, route.edges[1:] + route.edges[:1], strict=True):
            assert iasi_net.segments[a].to_node == iasi_net.segments[b].from_node


def test_backbone_lines_run_along_the_streets_they_serve(iasi_net):
    def streets(ref):
        return {iasi_net.segments[e].name for e in iasi_net.route_by_ref[ref].edges}

    assert "Bd. Independenței" in streets("28") and "Bd. Dacia" in streets("28")  # Dacia - Copou
    assert "Șos. Nicolina" in streets("41")  # Blocuri Ciurea - Piața Independenței
    assert {"Bd. Carol I", "Bd. Alexandru cel Bun"} <= streets("52")  # a line OSM lists without any stops
    assert "Șos. Bucium" in streets("30") and "Str. Canta" in streets("30")  # Bucium - Canta
    for ref in BACKBONE:
        assert len(streets(ref)) >= 5, ref  # a real multi-street route, not a stub
        assert len(set(iasi_net.route_by_ref[ref].edges)) >= 20, ref


def test_routes_come_from_osm_relations_and_run_along_their_streets():
    routes = json.loads((DATA_DIR / "iasi_routes.json").read_text(encoding="utf-8"))["routes"]
    by_ref = {r["ref"]: r for r in routes}
    assert all(r["osm_relations"] for r in routes)
    graph, lines = load_iasi()
    names = {e.id: e.name for e in graph.edges}
    on = {n for e in next(r for r in lines if r.ref == "28").edges if (n := names[e])}
    assert "Bd. Independenței" in on  # line 28 runs along the boulevard
    assert by_ref["28"]["from"] and by_ref["28"]["to"]


def test_gates_exist_and_are_mutually_reachable(iasi_net):
    net = iasi_net
    assert 12 <= len(net.gates) <= net.cfg.max_gates  # spread all round the edge of the larger city
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
