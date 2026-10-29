"""トイレとコンビニの場所（営業圏ごと）。タクシー運転手の「トイレどこ？」用（2026-10-29 社長指示）。

元データ＝OpenStreetMap（ODbL 1.0・出典をJSONに明示）。公衆トイレ（amenity=toilets）と
コンビニ（shop=convenience）を、営業圏の**形**（route_boundary_feed の ward_frame＝市区町の境界）で
切り抜く（四角で切ると隣の営業圏の物が混ざる＝decision-area-clip-by-shape-not-bbox）。

コンビニが「トイレを貸すか・時間帯で断られるか」は OSM にほぼ無い（福岡市内816店中2店）ので、
アプリで運転手どうしが報告して共有する（名前は出さない）。ここでは場所だけを配る。

  python build_toilets.py fukuoka   # → out/toilets/fukuoka.json
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time

import urllib.parse
import urllib.request

UA = "route_poi_feed/1.0 (+https://github.com/taxilog-app/route_poi_feed)"
OVERPASS_EPS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
HERE = os.path.dirname(os.path.abspath(__file__))
BOUNDARY = os.path.join(HERE, "..", "route_boundary_feed")
# 取得崩れで極端に少ない件数を配信しない（前回分を温存）
MIN_PLACES = 200

# コンビニの看板（名前・ブランドから。アプリの色分け用）
CHAINS = [
    ("seven", ["セブン", "7-eleven", "seven"]),
    ("family", ["ファミリーマート", "familymart", "ファミマ"]),
    ("lawson", ["ローソン", "lawson"]),
    ("ministop", ["ミニストップ", "ministop"]),
    ("poplar", ["ポプラ", "poplar"]),
    ("daily", ["デイリーヤマザキ", "daily yamazaki", "ヤマザキ"]),
]


def load_area_shape(area: str) -> list[list[list[tuple[float, float]]]]:
    """営業圏の形（多角形の並び・外周と穴）。座標は (lng, lat)。"""
    local = os.path.join(BOUNDARY, area, "ward_frame.geojson")
    if os.path.exists(local):
        with open(local, encoding="utf-8") as f:
            fc = json.load(f)
    else:  # CI＝境界のハブの配信から取る
        url = f"https://taxilog-app.github.io/route_boundary_feed/{area}/ward_frame.geojson"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}),
                                    timeout=60) as r:
            fc = json.loads(r.read().decode("utf-8"))
    polys = []
    for ft in fc["features"]:
        g = ft["geometry"]
        rings_list = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        for rings in rings_list:
            polys.append([[(p[0], p[1]) for p in r] for r in rings])
    return polys


def _in_ring(x: float, y: float, ring) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def in_area(lng: float, lat: float, polys) -> bool:
    for rings in polys:
        if _in_ring(lng, lat, rings[0]) and not any(_in_ring(lng, lat, h) for h in rings[1:]):
            return True
    return False


def bbox_of(polys) -> tuple[float, float, float, float]:
    xs = [p[0] for rings in polys for p in rings[0]]
    ys = [p[1] for rings in polys for p in rings[0]]
    return (min(ys), min(xs), max(ys), max(xs))  # south, west, north, east


# 全体の時間の上限（これを過ぎたら前回の配信／種の一覧を配る＝CIが何十分も止まらないように）
TIME_BUDGET_SEC = 300
_START = time.monotonic()


def _over_budget() -> bool:
    return time.monotonic() - _START > TIME_BUDGET_SEC


def _query(bbox, tries: int = 4) -> list[dict] | None:
    s, w, n, e = bbox
    q = (f'[out:json][timeout:90];('
         f'nwr["amenity"="toilets"]({s},{w},{n},{e});'
         f'nwr["shop"="convenience"]({s},{w},{n},{e}););out center tags;')
    last = None
    for attempt in range(tries):
        if _over_budget():
            print("[OSM] 時間の上限を過ぎました", file=sys.stderr)
            return None
        ep = OVERPASS_EPS[attempt % len(OVERPASS_EPS)]
        try:
            req = urllib.request.Request(
                ep, data=urllib.parse.urlencode({"data": q}).encode(),
                headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode("utf-8")
            if text.lstrip().startswith("{"):
                return json.loads(text).get("elements", [])
            last = text[:80]
        except Exception as ex:  # noqa: BLE001
            last = f"{type(ex).__name__} {ex}"
        wait = 5 * (attempt + 1)
        print(f"[OSM] {ep} 失敗({last}) {wait}s後リトライ…", file=sys.stderr)
        time.sleep(wait)
    print(f"[OSM] このます目は取れず: {last}", file=sys.stderr)
    return None


def fetch(bbox, step: float = 0.1) -> list[dict] | None:
    """営業圏の外枠を約10kmのます目に分けて小さく問い合わせる（一度に大きく頼むと
    混んでいる時に全部時間切れになった＝2026-10-29 CIで8回とも504）。1つでも取れなければ None。"""
    s, w, n, e = bbox
    els: list[dict] = []
    lat = s
    cells = 0
    while lat < n:
        lng = w
        while lng < e:
            got = _query((lat, lng, min(lat + step, n), min(lng + step, e)))
            if got is None:
                return None
            els.extend(got)
            cells += 1
            time.sleep(1)
            lng += step
        lat += step
    print(f"[OSM] {cells}ます -> {len(els)}件")
    return els


def carry_over(area: str, out_path: str) -> bool:
    """取れなかった時は前回配信した一覧をそのまま配り直す（トイレが急に消えないように）。"""
    url = f"https://taxilog-app.github.io/route_poi_feed/toilets/{area}.json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}),
                                    timeout=60) as r:
            raw = r.read()
        json.loads(raw.decode("utf-8"))
        with open(out_path, "wb") as f:
            f.write(raw)
        print(f"[CARRY] 前回の一覧を配り直します（{len(raw) // 1024}KB）")
        return True
    except Exception as ex:  # noqa: BLE001
        print(f"[CARRY] 前回の配信は無し: {ex}", file=sys.stderr)
    # 最後の手＝リポジトリに置いた種（初回の配信や、配信が消えた時のため）
    seed = os.path.join(HERE, "sources", "toilets", f"{area}.json")
    if os.path.exists(seed):
        with open(seed, "rb") as f:
            raw = f.read()
        with open(out_path, "wb") as f:
            f.write(raw)
        print(f"[CARRY] 種の一覧を配ります（{len(raw) // 1024}KB）")
        return True
    return False


def chain_of(t: dict) -> str:
    s = " ".join(str(t.get(k, "")) for k in ("brand", "name", "name:ja", "brand:ja", "operator")).lower()
    for key, words in CHAINS:
        if any(w.lower() in s for w in words):
            return key
    return "other"


def to_place(el: dict) -> dict | None:
    t = el.get("tags", {})
    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lng = el.get("lon") or (el.get("center") or {}).get("lon")
    if lat is None or lng is None:
        return None
    is_toilet = t.get("amenity") == "toilets"
    # 一般の人が使えないトイレ（私有・客専用）は出さない
    if is_toilet and t.get("access") in ("private", "no", "customers", "permissive_no"):
        return None
    name = t.get("name:ja") or t.get("name") or ""
    branch = t.get("branch")
    if branch and branch not in name:
        name = f"{name} {branch}".strip()
    p = {
        "id": f"{el['type'][0]}{el['id']}",  # 共有の報告の目印（OSMの番号）
        "k": "t" if is_toilet else "c",  # t=トイレ / c=コンビニ
        "n": name,
        "lat": round(float(lat), 6),
        "lng": round(float(lng), 6),
    }
    if not is_toilet:
        p["ch"] = chain_of(t)
    if t.get("wheelchair") in ("yes", "limited"):
        p["wc"] = 1  # 車いす・多目的
    oh = t.get("opening_hours")
    if oh:
        p["oh"] = oh[:60]
    return p


def main(area: str) -> int:
    polys = load_area_shape(area)
    out_dir = os.path.join(HERE, "out", "toilets")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{area}.json")
    els = fetch(bbox_of(polys))
    if els is None:
        return 0 if carry_over(area, path) else 1
    places, seen = [], set()
    for el in els:
        p = to_place(el)
        if not p or p["id"] in seen:
            continue
        if not in_area(p["lng"], p["lat"], polys):
            continue
        seen.add(p["id"])
        places.append(p)
    places.sort(key=lambda p: (p["k"], p["lat"], p["lng"]))
    nt = sum(1 for p in places if p["k"] == "t")
    nc = len(places) - nt
    print(f"[{area}] トイレ{nt} コンビニ{nc}（形で切り抜き後）")
    if len(places) < MIN_PLACES:
        print(f"[GUARD] {len(places)}件 < 下限{MIN_PLACES} → 前回の一覧を配り直す", file=sys.stderr)
        return 0 if carry_over(area, path) else 1
    out = {
        "schemaVersion": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "area": area,
        "attribution": "© OpenStreetMap contributors (ODbL 1.0)",
        "license": "https://opendatacommons.org/licenses/odbl/1-0/",
        "places": places,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"→ {path} ({os.path.getsize(path) // 1024}KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "fukuoka"))
