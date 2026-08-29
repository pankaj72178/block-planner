"""Build the section topology: stations -> blockable stretches -> asset segments.

Real inputs   : station codes / names / chainage (public IR time-tables, OSM).
Assumptions   : loop availability where OSM is incomplete, per-segment asset
                age / cumulative GMT / curvature (randomised inside realistic
                ranges, seeded so every run is reproducible).

`fetch_osm_topology()` holds the live Overpass query from the README. It is
NOT called by the pipeline -- the demo runs fully offline from section.yaml,
which is the point. Run it once yourself to cross-check chainage, cache the
result under data/osm_cache.json, and cite it in DATA.md.
"""
from __future__ import annotations

import random
from typing import Dict, List, Tuple

from core.model import Station, Stretch, AssetSegment

OVERPASS_QUERY = """
[out:json][timeout:120];
(
  way["railway"="rail"]({{bbox}});
  node["railway"="station"]({{bbox}});
);
out geom;
"""


def fetch_osm_topology(bbox: str, cache: str = "data/osm_cache.json") -> dict:
    """Optional, online. Kept so the repo shows the real provenance path."""
    import json
    import os
    import urllib.request

    if os.path.exists(cache):
        with open(cache) as f:
            return json.load(f)
    q = OVERPASS_QUERY.replace("{{bbox}}", bbox)
    req = urllib.request.Request(
        "https://overpass-api.de/api/interpreter", data=q.encode()
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        payload = json.loads(r.read().decode())
    with open(cache, "w") as f:
        json.dump(payload, f)
    return payload


def build_stations(cfg: dict) -> List[Station]:
    return [Station(**s) for s in cfg["stations"]]


def build_stretches(cfg: dict, stations: List[Station]) -> List[Stretch]:
    """One blockable stretch per inter-station gap per line."""
    out: List[Stretch] = []
    for line in cfg["section"]["lines"]:
        for a, b in zip(stations, stations[1:]):
            out.append(
                Stretch(
                    id=f"{a.code}-{b.code}/{line}",
                    from_code=a.code,
                    to_code=b.code,
                    line=line,
                    km_start=a.km,
                    km_end=b.km,
                )
            )
    return out


def build_asset_segments(
    cfg: dict, stations: List[Station], stretches: List[Stretch], seed: int = 7
) -> List[AssetSegment]:
    """~1 km asset segments. Age / GMT / curvature randomised in realistic bands.

    Two deliberate realism choices:
      * cumulative GMT is higher near the busy end of the section, because
        traffic is not uniform along a trunk route;
      * a segment inside a station yard gets flagged -- points & crossings
        maintenance only applies there.
    """
    rng = random.Random(seed)
    yard_half_km = 0.8
    yard_kms = [s.km for s in stations]
    base_gmt = cfg["section"]["gmt"]
    segs: List[AssetSegment] = []

    for st in stretches:
        lo, hi = st.km_start, st.km_end
        n = max(1, int(round(hi - lo)))
        step = (hi - lo) / n
        for i in range(n):
            k0 = lo + i * step
            k1 = k0 + step
            mid = (k0 + k1) / 2
            in_yard = any(abs(mid - y) <= yard_half_km for y in yard_kms)
            # traffic taper: slightly denser towards Surat (Mumbai end)
            gmt = base_gmt * (0.9 + 0.25 * (mid / max(1.0, hi)))
            age = rng.uniform(4, 28)
            segs.append(
                AssetSegment(
                    id=f"{st.id}@{mid:.1f}",
                    stretch_id=st.id,
                    line=st.line,
                    km_start=round(k0, 2),
                    km_end=round(k1, 2),
                    age_years=round(age, 1),
                    gmt_per_year=round(gmt, 1),
                    cumulative_gmt=round(gmt * age * rng.uniform(0.75, 1.0), 1),
                    curvature=round(max(0.0, rng.gauss(0.6, 0.8)), 2),
                    is_station_yard=in_yard,
                )
            )
    return segs


def stretch_index(stretches: List[Stretch]) -> Dict[str, Stretch]:
    return {s.id: s for s in stretches}


def stretch_for_km(
    stretches: List[Stretch], km: float, line: str
) -> Stretch | None:
    for s in stretches:
        if s.line == line and s.km_start <= km <= s.km_end:
            return s
    return None


def build_network(cfg: dict, seed: int = 7) -> Tuple[
    List[Station], List[Stretch], List[AssetSegment]
]:
    stations = build_stations(cfg)
    stretches = build_stretches(cfg, stations)
    segments = build_asset_segments(cfg, stations, stretches, seed=seed)
    return stations, stretches, segments
