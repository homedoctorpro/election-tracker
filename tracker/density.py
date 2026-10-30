"""Population density inside each 2026 House district, for the 3D density page (density.html).

Built once and committed to data/reference/density/ (one file per district plus index.json); the
site build copies the folder into site/density/.

    python -m tracker.density --kontur <kontur_population_US_*.gpkg> --simplemaps <zip>

Sources:
  Kontur Population (CC BY 4.0): people per H3 hexagon at resolution 8 (about 0.74 km² each), from
    census counts spread over building footprints; Nov 2023 release, one GeoPackage for the US:
    https://data.humdata.org/organization/kontur
  district lines: Census 119th-Congress cartographic file (1:500k) for states that kept their map,
    the SimpleMaps 120th-Congress basic file (CC BY 4.0) for the nine that redrew (REDRAWN). The
    SimpleMaps file is coarse, so hexes near a redrawn district's edge can land in its neighbor.

Needs h3, shapely and pyshp (pip install h3 shapely pyshp); none of them run in CI.
"""
import argparse
import json
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from .common import fetch
from .race_map import FIPS, REDRAWN, REF, district_key, load_pres

OUT = REF / "density"
CENSUS_CD_500K = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_cd119_500k.zip"
KM2_TO_MI2 = 0.386102
# people per square mile; a hex's density is its population over its own area
BANDS = (500, 3000)
# districts with more populated res-8 hexes than this have their sparse hexes merged (see
# _coarsen) to keep the file small; stats are always computed at res 8 first
MAX_CELLS = 8000


def _shapes(shp, key_of):
    import shapefile
    from shapely.geometry import shape
    r = shapefile.Reader(str(shp))
    out = {}
    for sr in r.iterShapeRecords():
        key = key_of(sr.record.as_dict())
        if key:
            out[key] = shape(sr.shape.__geo_interface__).buffer(0)
    return out


def districts(simplemaps):
    """{key: shapely geometry in lon/lat} for every district on the 2026 lines."""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "cd.zip").write_bytes(fetch(CENSUS_CD_500K, timeout=300))
    zipfile.ZipFile(tmp / "cd.zip").extractall(tmp / "cd")
    zipfile.ZipFile(simplemaps).extractall(tmp / "sm")

    def census(rec):
        st = FIPS.get(rec["GEOID"][:2])
        if st and st not in REDRAWN and st != "DC" and rec["GEOID"][2:] != "ZZ":
            return district_key(st, rec["GEOID"][2:])

    def simple(rec):
        if rec["id"][:2] in REDRAWN:
            return district_key(rec["id"][:2], rec["id"][2:])

    geo = _shapes(next((tmp / "cd").glob("*.shp")), census)
    # the zip also has a pre-projected congress_albersusa.shp; we need the WGS84 one
    geo.update(_shapes(next(p for p in (tmp / "sm").rglob("*.shp") if "albers" not in p.name), simple))
    missing = sorted(set(load_pres()) - set(geo))
    assert not missing, f"no outline for {missing}"
    return geo


def _short(cell):
    """H3 index without its trailing f's (the unused digits); the page pads it back to 15."""
    return cell.rstrip("f")


def _coarsen(cells, pops):
    """Merge sparse hexes into their parents until the district fits MAX_CELLS: first the 7 res-8
    hexes under a res-7 parent (about 2 sq mi) when every one is under BANDS[0] people per sq mi,
    then res-7 hexes into res 6 (about 14 sq mi) when every one is under 100. Towns keep res 8."""
    import h3
    import numpy as np
    cur = dict(zip(cells, pops))
    for res, limit in ((7, BANDS[0]), (6, 100)):
        if len(cur) <= MAX_CELLS:
            break
        groups = {}
        for c, p in cur.items():
            groups.setdefault(h3.cell_to_parent(c, res), []).append((c, p))
        out = {}
        for parent, kids in groups.items():
            if all(h3.get_resolution(c) == res + 1 and p / (h3.cell_area(c, "km^2") * KM2_TO_MI2) < limit
                   for c, p in kids):
                out[parent] = sum(p for _, p in kids)
            else:
                out.update(kids)
        cur = out
    return list(cur), np.array(list(cur.values()))


def _outline(g):
    from shapely.geometry import mapping
    # ~150 m for a city district, up to ~2 km for Alaska's coastline
    s = g.simplify(min(0.02, max(0.0015, g.area ** 0.5 * 0.002)), preserve_topology=True)
    m = mapping(s)
    polys = m["coordinates"] if m["type"] == "MultiPolygon" else [m["coordinates"]]
    return [[[[round(x, 4), round(y, 4)] for x, y in ring] for ring in poly] for poly in polys]


def build(kontur, simplemaps):
    import h3
    import numpy as np
    import shapely

    geo = districts(simplemaps)
    keys = sorted(geo)
    tree = shapely.STRtree([geo[k] for k in keys])

    rows = sqlite3.connect(kontur).execute("SELECT h3, population FROM population").fetchall()
    cells = [r[0] for r in rows]
    pop = np.array([r[1] for r in rows])
    ll = np.array([h3.cell_to_latlng(c) for c in cells])
    pts = shapely.points(ll[:, 1], ll[:, 0])
    pt_i, geo_i = tree.query(pts, predicate="within")
    owner = np.full(len(cells), -1)
    owner[pt_i] = geo_i
    # hex centers just outside a (simplified) coastline or a coarse redrawn edge: nearest district
    # within ~2 km; anything farther (Puerto Rico, territories) is left out
    loose = np.flatnonzero(owner < 0)
    li, gi = tree.query_nearest(pts[loose], max_distance=0.02)
    owner[loose[li]] = gi
    print(f"{len(cells):,} hexes, {pop.sum():,.0f} people; {len(li):,} placed by nearest district; "
          f"left out {(owner < 0).sum():,} ({pop[owner < 0].sum():,.0f} people)")

    OUT.mkdir(parents=True, exist_ok=True)
    index = {}
    order = np.argsort(owner, kind="stable")
    bounds = np.searchsorted(owner[order], np.arange(len(keys) + 1))
    for gi_, key in enumerate(keys):
        idx = order[bounds[gi_]:bounds[gi_ + 1]]
        cs, ps = [cells[i] for i in idx], pop[idx]
        areas = np.array([h3.cell_area(c, "km^2") for c in cs]) if len(cs) else np.array([1.0])
        dens = ps / (areas * KM2_TO_MI2)
        total = ps.sum()
        shares = [float(ps[dens < BANDS[0]].sum() / total),
                  float(ps[(dens >= BANDS[0]) & (dens < BANDS[1])].sum() / total),
                  float(ps[dens >= BANDS[1]].sum() / total)]
        g = geo[key]
        stats = {"pop": int(round(total)),
                 # density where the typical resident lives: each hex's density weighted by its people
                 "pwd": int(round(float((dens * ps).sum() / total))),
                 "bands": [round(s, 3) for s in shares],
                 "populated_mi2": int(round(float(areas.sum()) * KM2_TO_MI2))}
        cs, ps = _coarsen(cs, ps)
        res = min(h3.get_resolution(c) for c in cs)  # coarsest hex shown
        index[key] = {**stats, "res": res, "cells": len(cs), "bbox": [round(v, 3) for v in g.bounds]}
        doc = {"key": key, "res": res, **stats, "bbox": index[key]["bbox"], "outline": _outline(g),
               "cells": [[_short(c), int(round(p))] for c, p in zip(cs, ps) if p >= 1]}
        (OUT / f"{key}.json").write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    (OUT / "index.json").write_text(json.dumps({"source": "Kontur Population 2023-11-01 (CC BY 4.0)",
                                                "bands": BANDS, "districts": index}, separators=(",", ":")),
                                    encoding="utf-8")
    print(f"wrote {len(keys)} districts to {OUT}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--kontur", required=True, help="kontur_population_US_*.gpkg (unzipped)")
    ap.add_argument("--simplemaps", required=True, help="SimpleMaps congress basic zip (120th Congress lines)")
    a = ap.parse_args()
    build(a.kontur, a.simplemaps)
