#!/usr/bin/env python3
"""route_poi_feed — ホテルPOIフィード試作（OSM ∪ Wikidata 合体版）。

設計（壁打ち確定 2026-07-15）:
  ① OSMとWikidataの2ソースを重ねる → 抜けを減らし、名前はWikidata正式名を優先
  ② 支店名(○○店/場所)は消さず一貫した書式で必ず付ける
  ③ 各ホテルに信頼度 conf(verified=2ソース一致 / single=片方)を付ける
  ④(将来) 運転手の1タップ修正をFirebaseで上乗せ

出力: out/hotels_jp.json  {schemaVersion, generatedAt, region, hotels:[...]}
本番はGitHub Actionsで週1実行→GitHub Pages配信。ローカルは試作/検証用。
"""
from __future__ import annotations
import os, json, math, time, sys, datetime as dt, unicodedata, re
import requests

# 連絡先は公開リポURLで示す（個人メールは公開リポに出さない＝プライバシー保護）。
UA = "route_poi_feed/0.1 (+https://github.com/kirinkatanaboy-spec/route_poi_feed)"

# 公開ガード: 取得崩れで極端に少ない件数を配信しないための下限（CIを失敗させ前回分を温存）。
MIN_HOTELS = 50

# 福岡中心部（天神〜博多駅）。将来は営業圏ごとに増やす（流動要素）。
REGION = "fukuoka_central"
BBOX = (33.575, 130.388, 33.605, 130.428)  # south, west, north, east

OVERPASS_EPS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

# tourism種別 → 出力type
TYPE_MAP = {
    "hotel": "hotel", "hostel": "hostel", "guest_house": "guest_house",
    "motel": "hotel", "apartment": "apartment", "love_hotel": "love_hotel",
}


# ─────────────────────────── OSM ───────────────────────────
def fetch_osm(bbox) -> list[dict]:
    s, w, n, e = bbox
    tourism = "|".join(TYPE_MAP.keys())
    q = (f'[out:json][timeout:60];'
         f'(node["tourism"~"^({tourism})$"]({s},{w},{n},{e});'
         f'way["tourism"~"^({tourism})$"]({s},{w},{n},{e}););out center tags;')
    last = None
    for attempt in range(6):
        ep = OVERPASS_EPS[attempt % len(OVERPASS_EPS)]
        try:
            r = requests.post(ep, data={"data": q},
                              headers={"User-Agent": UA}, timeout=90)
            if r.status_code == 200 and r.text.lstrip().startswith("{"):
                els = r.json().get("elements", [])
                print(f"[OSM] {ep} -> {len(els)}件")
                return _osm_to_rows(els)
            last = f"{r.status_code} {r.text[:80]!r}"
        except Exception as ex:
            last = f"{type(ex).__name__} {ex}"
        wait = 5 * (attempt + 1)
        print(f"[OSM] {ep} 失敗({last}) {wait}s後リトライ…", file=sys.stderr)
        time.sleep(wait)
    print(f"[OSM] 全滅: {last} → OSM無しで続行(縮退)", file=sys.stderr)
    return []


def _compose_osm_name(t: dict) -> tuple[str | None, bool]:
    """OSMタグから最も具体的な日本語名を組み立てる。
    返り値 (name, brand_only)。brand_only=支店名が取れずブランドのみ。"""
    brand = t.get("brand")
    branch = t.get("branch")
    # 候補=日本語名寄りの中から最長（=最も具体的）を採用
    cands = [t.get("name:ja"), t.get("name"), t.get("official_name")]
    cands = [_norm_name(c) for c in cands if c]
    best = max(cands, key=len) if cands else None
    nbrand = _norm_name(brand) if brand else None
    # ブランドだけ(=支店情報が名前に無い)なら branch を合成
    if branch and (best is None or best == nbrand):
        return _norm_name(f"{brand or best} {branch}"), False
    if best is None and brand:
        return nbrand, True
    if best is not None and nbrand and best == nbrand:
        return best, True  # 真の空白（branchも無い）→アプリが町丁目で補う
    return best, False


def _osm_to_rows(els) -> list[dict]:
    rows = []
    for el in els:
        t = el.get("tags", {})
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lng = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lng is None:
            continue
        name, brand_only = _compose_osm_name(t)
        rows.append({
            "src": "osm",
            "osm_id": f'{el["type"]}/{el["id"]}',
            "lat": float(lat), "lng": float(lng),
            "name": name, "brand_only": brand_only,
            "type": TYPE_MAP.get(t.get("tourism"), "hotel"),
            "wikidata": t.get("wikidata"),
        })
    return rows


# ─────────────────────────── Wikidata ───────────────────────────
def fetch_wikidata(bbox) -> list[dict]:
    s, w, n, e = bbox
    # 長方形box内・Q27686(hotel)配下。日本語ラベル(無ければQ-idにフォールバック=穴として検出)
    sparql = f"""
SELECT ?item ?itemLabel ?lat ?lon WHERE {{
  SERVICE wikibase:box {{
    ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:cornerSouthWest "Point({w} {s})"^^geo:wktLiteral .
    bd:serviceParam wikibase:cornerNorthEast "Point({e} {n})"^^geo:wktLiteral .
  }}
  ?item wdt:P31/wdt:P279* wd:Q27686 .
  ?item p:P625/psv:P625 ?cn .
  ?cn wikibase:geoLatitude ?lat . ?cn wikibase:geoLongitude ?lon .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "ja". }}
}}"""
    for attempt in range(4):
        try:
            r = requests.get(WIKIDATA_SPARQL, params={"query": sparql, "format": "json"},
                             headers={"User-Agent": UA}, timeout=60)
            if r.status_code == 200:
                bind = r.json()["results"]["bindings"]
                rows, seen = [], set()
                for b in bind:
                    qid = b["item"]["value"].rsplit("/", 1)[-1]
                    if qid in seen:
                        continue
                    seen.add(qid)
                    label = b["itemLabel"]["value"]
                    # ja名が無いと itemLabel が Q\d+ に化ける → 名前無し扱い
                    has_name = not re.fullmatch(r"Q\d+", label)
                    rows.append({
                        "src": "wd", "qid": qid,
                        "lat": float(b["lat"]["value"]), "lng": float(b["lon"]["value"]),
                        "name": label if has_name else None,
                    })
                print(f"[WD] {len(rows)}件 (うち名前あり {sum(1 for x in rows if x['name'])})")
                return rows
        except Exception as ex:
            print(f"[WD] 失敗 {type(ex).__name__} {ex} リトライ…", file=sys.stderr)
        time.sleep(5 * (attempt + 1))
    print("[WD] 全滅 → Wikidata無しで続行(縮退)", file=sys.stderr)
    return []


# ─────────────────────────── マージ ───────────────────────────
def _dist_m(a_lat, a_lng, b_lat, b_lng) -> float:
    R = 6371000
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lng - a_lng)
    x = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(x))


def _norm_name(s: str) -> str:
    # 全角/半角ゆらぎを統一（NFKC）＋空白畳み。支店名は消さない。
    s = unicodedata.normalize("NFKC", s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


_JP_RE = re.compile(r"[ぁ-んァ-ヶ一-龥]")


def _has_jp(s: str | None) -> bool:
    return bool(s and _JP_RE.search(s))


def _pick_name(osm_name, osm_brand_only, wd_name):
    """表示名を選ぶ。日本語(現地の呼び名/正式名)を最優先し、日本語候補の中では
    最も具体的(長い)ものを採る。英語しか無いときだけ英語。返り値 (name, brand_only)。"""
    jp = [c for c in (osm_name, wd_name) if _has_jp(c)]
    if jp:
        name = max(jp, key=len)  # 日本語の中で最も具体的
        # 選ばれた名前がブランドのみ(=OSMの空白名しか無い)ときだけフラグ維持
        still_bare = osm_brand_only and name == osm_name and not _has_jp(wd_name)
        return name, still_bare
    # 英語しか無い
    if osm_name:
        return osm_name, osm_brand_only
    if wd_name:
        return wd_name, False
    return None, osm_brand_only


def merge(osm: list[dict], wd: list[dict], match_m=60) -> list[dict]:
    """OSMを基盤に、Wikidataを重ねる。
    照合順: ①OSMのwikidataタグ=QID一致 → ②近接(match_m以内)。
    名前は Wikidata正式名 > OSM名。両ソース一致=verified。"""
    wd_by_qid = {x["qid"]: x for x in wd}
    wd_used = set()
    out = []

    # 基盤 = OSM（あれば）
    for o in osm:
        w = None
        if o.get("wikidata") and o["wikidata"] in wd_by_qid:
            w = wd_by_qid[o["wikidata"]]
        if w is None:  # 近接照合
            best, bestd = None, match_m + 1
            for x in wd:
                if x["qid"] in wd_used:
                    continue
                d = _dist_m(o["lat"], o["lng"], x["lat"], x["lng"])
                if d < bestd:
                    best, bestd = x, d
            if best is not None:
                w = best
        conf = "single"
        osm_name = o.get("name")
        brand_only = o.get("brand_only", False)
        if w is not None:
            wd_used.add(w["qid"])
            conf = "verified"
            # 名前選択の優先: ①OSMの具体的な日本語名(現地の呼び名) →
            #   ②WDの日本語正式名で穴埋め → ③OSMの合成/英語 → ④WD英語
            name, brand_only = _pick_name(osm_name, brand_only, w.get("name"))
        else:
            name = osm_name
        if not name:
            continue  # 名前が取れないものは出さない（名前チップが作れない）
        row = {
            "name": _norm_name(name), "lat": round(o["lat"], 6), "lng": round(o["lng"], 6),
            "type": o["type"], "conf": conf,
            "ref": o["osm_id"] + (f"+wd:{w['qid']}" if w else ""),
        }
        if brand_only:
            row["brandOnly"] = True  # アプリが町丁目を付けて区別する対象
        out.append(row)

    # OSMに無く Wikidataだけにある（名前ありのみ）＝網羅を足す
    for x in wd:
        if x["qid"] in wd_used or not x.get("name"):
            continue
        out.append({
            "name": _norm_name(x["name"]), "lat": round(x["lat"], 6), "lng": round(x["lng"], 6),
            "type": "hotel", "conf": "single", "ref": f"wd:{x['qid']}",
        })

    # 近接重複除去。verified優先で残す。
    #  同一正式名は150m以内なら同一施設(node+wayや2ソース)とみなし結合。
    #  別支店は名前が異なる(例「東横イン 博多駅南」≠「東横イン 博多口駅前」)ので誤結合しない。
    out.sort(key=lambda r: (r["conf"] != "verified", not r["name"], len(r["name"])), reverse=False)
    dedup = []
    for r in out:
        dup = False
        for k in dedup:
            d = _dist_m(r["lat"], r["lng"], k["lat"], k["lng"])
            same = r["name"] == k["name"]
            contain = r["name"] in k["name"] or k["name"] in r["name"]
            if (same and d < 150) or (contain and d < 30):
                dup = True
                break
        if not dup:
            dedup.append(r)
    return dedup


# ─────────────────────────── build ───────────────────────────
def main():
    osm = fetch_osm(BBOX)
    wd = fetch_wikidata(BBOX)
    hotels = merge(osm, wd)
    # 公開ガード: 下限未満はデータ崩れとみなし配信しない（CI失敗→Pagesは前回分を温存）。
    if len(hotels) < MIN_HOTELS:
        print(f"[GUARD] {len(hotels)}件 < 下限{MIN_HOTELS} → 配信中止(異常)", file=sys.stderr)
        sys.exit(1)
    feed = {
        "schemaVersion": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        # 出典・ライセンス明示（ODbL遵守）。配信物そのものに持たせる。
        "license": "Data: ODbL 1.0 (OpenStreetMap) + CC0 (Wikidata)",
        "attribution": "© OpenStreetMap contributors / Wikidata",
        "region": REGION,
        "hotels": sorted(hotels, key=lambda r: (r["lat"], r["lng"])),
    }
    os.makedirs("out", exist_ok=True)
    with open("out/hotels_jp.json", "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, separators=(",", ":"))

    v = sum(1 for h in hotels if h["conf"] == "verified")
    print(f"\n=== 出力 out/hotels_jp.json : {len(hotels)}件 "
          f"(verified={v} / single={len(hotels)-v}) ===")
    print("--- 主要どころ抜粋 ---")
    for h in hotels:
        if h["conf"] == "verified":
            print(f"  [{h['conf']:8}] {h['name']}  ({h['type']})")


if __name__ == "__main__":
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main()
