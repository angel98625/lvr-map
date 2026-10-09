"""下載內政部「鄉鎮市區界線」，簡化後依縣市輸出 bounds_{代碼}.json，讓行情分區地圖可以塗滿整個行政區。

需要 pyshp（pip install pyshp）。輸出格式：{"行政區": [[[lng, lat], ...], ...]}（每個行政區可能有多塊）。
"""
import io
import json
import os
import sys
import urllib.request
import zipfile


OUT = os.environ.get("OUT", "out")
# 先試 npm 套件 taiwan-atlas（內政部界線轉成的 TopoJSON），再試內政部原始 shapefile
TOPO_URLS = [
    "https://cdn.jsdelivr.net/npm/taiwan-atlas/towns-10t.json",
    "https://unpkg.com/taiwan-atlas/towns-10t.json",
]
URLS = [
    # 鄉鎮市區界線(TWD97經緯度)
    "https://data.moi.gov.tw/MoiOD/System/DownloadFile.aspx?DATA=CD02C824-45C5-48C8-B631-98B205A2E35A",
]
CODES = {
    "臺北市": "A", "新北市": "F", "桃園市": "H", "臺中市": "B", "臺南市": "D", "高雄市": "E", "基隆市": "C", "新竹市": "O",
    "新竹縣": "J", "苗栗縣": "K", "彰化縣": "N", "南投縣": "M", "雲林縣": "P", "嘉義市": "I", "嘉義縣": "Q", "屏東縣": "T",
    "宜蘭縣": "G", "花蓮縣": "U", "臺東縣": "V", "澎湖縣": "X", "金門縣": "W", "連江縣": "Z",
}
TOL = 0.00015  # 約 15 公尺


def simplify(pts, tol):
    """Douglas–Peucker（非遞迴）。"""
    if len(pts) < 4:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        norm = (dx * dx + dy * dy) ** 0.5 or 1e-12
        best, bi = -1, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > best:
                best, bi = d, i
        if best > tol:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (lvr-map bounds)"})
    return urllib.request.urlopen(req, timeout=120).read()


def pick_key(props, words):
    for k in props:
        if any(w in k.upper() for w in words):
            return k
    return None


def from_topo(topo):
    """TopoJSON → {(縣市, 行政區): [ring, ...]}"""
    tf = topo.get("transform")
    arcs = []
    for arc in topo["arcs"]:
        pts, x, y = [], 0, 0
        for p in arc:
            if tf:
                x, y = x + p[0], y + p[1]
                pts.append((x * tf["scale"][0] + tf["translate"][0], y * tf["scale"][1] + tf["translate"][1]))
            else:
                pts.append((p[0], p[1]))
        arcs.append(pts)

    def ring(idx):
        out = []
        for i in idx:
            a = arcs[i] if i >= 0 else arcs[~i][::-1]
            out.extend(a if not out else a[1:])
        return out

    res = {}
    for name, obj in topo["objects"].items():
        geoms = obj.get("geometries", [obj])
        if geoms:
            print("圖層", name, len(geoms), "個，欄位：", geoms[0].get("properties"))
        for g in geoms:
            pr = g.get("properties") or {}
            ck, tk = pick_key(pr, ["COUNTY"]), pick_key(pr, ["TOWNNAME", "TOWN_NAME", "TOWN"])
            if not ck or not tk:
                continue
            polys = [g["arcs"]] if g["type"] == "Polygon" else g["arcs"] if g["type"] == "MultiPolygon" else []
            rings = [ring(r) for poly in polys for r in poly]
            res.setdefault((str(pr[ck]), str(pr[tk])), []).extend(rings)
    return res


def write(shapes):
    out = {}
    for (county, town), rings in shapes.items():
        code = CODES.get(county.replace("台", "臺"))
        if not code:
            continue
        for pts in rings:
            r = simplify(pts, TOL)
            if len(r) >= 4:
                out.setdefault(code, {}).setdefault(town, []).append([[round(x, 5), round(y, 5)] for x, y in r])
    os.makedirs(OUT, exist_ok=True)
    for code, towns in out.items():
        with open(os.path.join(OUT, f"bounds_{code}.json"), "w", encoding="utf-8") as fh:
            json.dump(towns, fh, ensure_ascii=False, separators=(",", ":"))
        print(code, len(towns), "個行政區", sum(len(r) for t in towns.values() for r in t), "個點")
    return bool(out)


def main():
    for url in TOPO_URLS:
        try:
            if write(from_topo(json.loads(fetch(url)))):
                return
            print("沒有可用的縣市／行政區欄位", url)
        except Exception as e:  # noqa: BLE001
            print(f"下載或解析失敗 {url}：{e}")
    data = None
    for url in URLS:
        try:
            data = fetch(url)
            zipfile.ZipFile(io.BytesIO(data))
            break
        except Exception as e:  # noqa: BLE001
            print(f"下載失敗 {url}：{e}")
            data = None
    if not data:
        print("沒有拿到行政區界線，略過")
        return
    import shapefile  # pyshp

    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    print("壓縮檔內容：", names)
    shp = next(n for n in names if n.lower().endswith(".shp"))
    base = shp[:-4]
    pick = lambda ext: io.BytesIO(zf.read(next(n for n in names if n.lower() == (base + ext).lower())))
    enc = "utf-8"
    cpg = [n for n in names if n.lower() == (base + ".cpg").lower()]
    if cpg:
        enc = zf.read(cpg[0]).decode("ascii", "ignore").strip() or "utf-8"
    r = shapefile.Reader(shp=pick(".shp"), dbf=pick(".dbf"), shx=pick(".shx"), encoding=enc if enc.lower() != "big5" else "cp950")
    fields = [f[0] for f in r.fields[1:]]
    print("欄位：", fields)
    ci = next(i for i, f in enumerate(fields) if f.upper().startswith("COUNTYNAME"))
    ti = next(i for i, f in enumerate(fields) if f.upper().startswith("TOWNNAME"))
    out = {}
    for sr in r.iterShapeRecords():
        county = str(sr.record[ci]).replace("台", "臺")
        town = str(sr.record[ti])
        code = CODES.get(county)
        if not code:
            continue
        pts, parts = sr.shape.points, list(sr.shape.parts) + [len(sr.shape.points)]
        rings = []
        for a, b in zip(parts, parts[1:]):
            ring = simplify(pts[a:b], TOL)
            if len(ring) >= 4:
                rings.append([[round(x, 5), round(y, 5)] for x, y in ring])
        out.setdefault(code, {}).setdefault(town, []).extend(rings)
    os.makedirs(OUT, exist_ok=True)
    for code, towns in out.items():
        with open(os.path.join(OUT, f"bounds_{code}.json"), "w", encoding="utf-8") as fh:
            json.dump(towns, fh, ensure_ascii=False, separators=(",", ":"))
        print(code, len(towns), "個行政區", sum(len(r) for t in towns.values() for r in t), "個點")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001  界線只是加分項，失敗不要讓整個更新失敗
        print("行政區界線處理失敗：", e)
        sys.exit(0)
