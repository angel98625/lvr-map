"""下載內政部實價登錄開放資料，整理成「行情查詢」網頁用的精簡 JSON。

輸出到 OUT 目錄（預設 out/）：
  index.json                縣市清單、各縣市筆數、資料更新日
  {代碼}_sale.json          買賣（成屋，不含預售屋）
  {代碼}_rent.json          租賃

每筆資料是一個陣列，欄位順序寫在 JSON 的 fields 裡。只用 Python 標準函式庫。
"""
import csv
import gzip
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.request
import zipfile

BASE = "https://plvr.land.moi.gov.tw"
OUT = os.environ.get("OUT", "out")
YEARS = int(os.environ.get("YEARS", "3"))

CITIES = {
    "a": "臺北市", "f": "新北市", "h": "桃園市", "b": "臺中市", "d": "臺南市", "e": "高雄市",
    "c": "基隆市", "o": "新竹市", "j": "新竹縣", "k": "苗栗縣", "n": "彰化縣", "m": "南投縣",
    "p": "雲林縣", "i": "嘉義市", "q": "嘉義縣", "t": "屏東縣", "g": "宜蘭縣", "u": "花蓮縣",
    "v": "臺東縣", "x": "澎湖縣", "w": "金門縣", "z": "連江縣",
}

# 建物型態 → 代碼（其餘型態如店面、辦公、工廠不收）
TYPES = [("公寓", 1), ("華廈", 2), ("住宅大樓", 3), ("套房", 4), ("透天厝", 5)]

SPECIAL_WORDS = [
    "親友", "員工", "特殊關係", "二親等", "親屬", "共有人", "急買急賣", "急售", "急買", "瑕疵", "凶宅", "非自然死亡",
    "政府機關", "法拍", "拍賣", "債權", "債務", "抵債", "地上權", "畸零地", "調處", "公共設施保留地",
    "民情風俗", "交換", "合併", "持分", "受贈", "贈與", "租約", "含租約", "附帶租約", "道路用地", "法院",
]
ADDON_WORDS = ["增建", "未登記建物", "頂樓加蓋", "加蓋"]

CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def log(*a):
    print(*a, flush=True)


def cn_num(s):
    """一 → 1、十一 → 11、二十三 → 23、一百零五 → 105；也接受阿拉伯數字。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if not s or any(c not in "一二三四五六七八九十百零〇" for c in s):
        return None
    n = 0
    if "百" in s:
        h, s = s.split("百", 1)
        n += (CN.get(h, 1) if h else 1) * 100
    s = s.replace("零", "").replace("〇", "")
    if "十" in s:
        t, s = s.split("十", 1)
        n += (CN.get(t, 1) if t else 1) * 10
    if s:
        n += CN.get(s, 0)
    return n or None


def parse_floors(text):
    """回傳 (地上樓層集合, 是否全棟)。地下層、騎樓、陽台等不算。"""
    text = text or ""
    whole = "全" in text
    floors = set()
    for tok in re.split(r"[，,、;；\s]+", text):
        m = re.fullmatch(r"(地下)?(第)?([一二三四五六七八九十百零〇\d]+)(層|樓)", tok)
        if m and not m.group(1):
            n = cn_num(m.group(3))
            if n:
                floors.add(n)
    return floors, whole


def roc_date(s):
    """1130315 → 20240315；只有年月（11303）時日設為 1。"""
    s = re.sub(r"\D", "", s or "")
    if len(s) < 5:
        return None
    if len(s) in (5, 6):  # 年月
        y, m, d = int(s[:-2]), int(s[-2:]), 1
    else:
        y, m, d = int(s[:-4]), int(s[-4:-2]), int(s[-2:])
    if not (1 <= m <= 12) or not (1 <= y <= 200):
        return None
    d = min(max(d, 1), 28) if not (1 <= d <= 31) else d
    return (y + 1911) * 10000 + m * 100 + d


def num(s):
    try:
        return float(str(s).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def http_get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (lvr-map price bot)"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            log(f"  下載失敗（第 {i + 1} 次）：{e}")
            time.sleep(5 * (i + 1))
    return None


def get_zip(url):
    data = http_get(url)
    if not data:
        return None
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        log(f"  不是 zip（{len(data)} bytes）：{data[:120]!r}")
        return None


def read_csv(zf, name):
    names = {n.lower(): n for n in zf.namelist()}
    real = names.get(name.lower())
    if not real:
        return None, []
    raw = zf.read(real)
    for enc in ("utf-8-sig", "cp950", "big5hkscs"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return None, []
    header = [re.sub(r"[\s()（）]", "", h) for h in rows[0]]
    body = rows[1:]
    if body and body[0] and body[0][0].lower().startswith("the "):  # 第二列是英文欄名
        body = body[1:]
    return header, body


class Cols:
    def __init__(self, header):
        self.h = header

    def idx(self, *keys):
        for k in keys:
            for i, h in enumerate(self.h):
                if h == k:
                    return i
        for k in keys:
            for i, h in enumerate(self.h):
                if h.startswith(k):
                    return i
        return -1


FULL = str.maketrans("０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ－～", "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-~")


def remark_flags(remark, rtype=""):
    f = 0
    # 租賃：分租、只租部分範圍或多人共同承租，單價會失真
    if "分租" in rtype or any(w in remark for w in ("部分範圍", "共同承租", "個別出租")):
        f |= 8
    if remark:
        f |= 4
        if any(w in remark for w in SPECIAL_WORDS):
            f |= 1
        if any(w in remark for w in ADDON_WORDS):
            f |= 2
    return f


def type_code(t):
    for k, v in TYPES:
        if t.startswith(k):
            return v
    return 0


SALE_FIELDS = ["區", "日期", "型態", "樓別", "頂層", "屋齡", "坪數", "總價", "單價", "備註旗標", "車位", "地址", "移轉層次", "總樓層", "房", "備註", "編號"]
RENT_FIELDS = ["區", "日期", "型態", "樓別", "頂層", "屋齡", "坪數", "租金", "單價", "備註旗標", "車位", "地址", "租賃層次", "總樓層", "房", "備註", "編號", "出租型態"]


def convert(header, body, kind, dist_index, min_date):
    c = Cols(header)
    I = {
        "dist": c.idx("鄉鎮市區"),
        "target": c.idx("交易標的"),
        "addr": c.idx("土地位置建物門牌", "土地區段位置建物區段門牌"),
        "date": c.idx("交易年月日") if kind == "sale" else c.idx("租賃年月日"),
        "floor": c.idx("移轉層次") if kind == "sale" else c.idx("租賃層次"),
        "tfloor": c.idx("總樓層數"),
        "btype": c.idx("建物型態"),
        "built": c.idx("建築完成年月"),
        "area": c.idx("建物移轉總面積平方公尺", "建物移轉總面積") if kind == "sale" else c.idx("建物總面積平方公尺", "建物總面積"),
        "rooms": c.idx("建物現況格局-房"),
        "price": c.idx("總價元") if kind == "sale" else c.idx("總額元"),
        "pkarea": c.idx("車位移轉總面積平方公尺", "車位移轉總面積") if kind == "sale" else c.idx("車位面積平方公尺", "車位面積"),
        "pkprice": c.idx("車位總價元") if kind == "sale" else c.idx("車位總額元"),
        "pktype": c.idx("車位類別"),
        "remark": c.idx("備註"),
        "id": c.idx("編號"),
        "rtype": c.idx("出租型態"),
    }
    missing = [k for k in ("dist", "date", "floor", "btype", "area", "price") if I[k] < 0]
    if missing:
        log(f"  找不到欄位 {missing}；欄名：{header}")
        return []

    def g(row, k):
        i = I[k]
        return row[i].strip() if 0 <= i < len(row) else ""

    out = []
    for row in body:
        if len(row) < 10:
            continue
        target = g(row, "target")
        if target and "建物" not in target and kind == "sale":
            continue
        t = type_code(g(row, "btype"))
        if not t:
            continue
        date = roc_date(g(row, "date"))
        if not date or date < min_date:
            continue
        area = num(g(row, "area"))
        price = num(g(row, "price"))
        if area <= 0 or price <= 0:
            continue
        pk_area, pk_price = num(g(row, "pkarea")), num(g(row, "pkprice"))
        has_pk = bool(g(row, "pktype")) or pk_area > 0
        # 扣除車位：車位有單獨標價時，總價與面積都扣掉車位
        if has_pk and pk_price > 0 and pk_area > 0 and area - pk_area > 3:
            area_n, price_n, pk = area - pk_area, price - pk_price, 1
        elif has_pk:
            area_n, price_n, pk = area - pk_area if area - pk_area > 3 else area, price, 2  # 車位沒有拆開標價
        else:
            area_n, price_n, pk = area, price, 0
        ping = area_n * 0.3025
        if ping < 1 or price_n <= 0:
            continue
        unit = round(price_n / ping)

        floor_txt = g(row, "floor")
        floors, whole = parse_floors(floor_txt)
        tfloor = cn_num(re.sub(r"(層|樓)$", "", g(row, "tfloor"))) or 0
        if whole or not floors:
            cat = 0
        elif floors == {1}:
            cat = 1
        elif len(floors) == 1:
            cat = 2
        else:
            cat = 3 if 1 in floors else 0  # 3：一樓連同樓上一起賣（例如一、二樓）
        top = 1 if (floors and tfloor and max(floors) >= tfloor and tfloor > 1) else 0

        built = roc_date(g(row, "built"))
        if built and built <= date:
            age = (dt.date(date // 10000, date // 100 % 100, 1) - dt.date(built // 10000, built // 100 % 100, 1)).days / 365.25
            age10 = max(0, round(age * 10))
        else:
            age10 = -1

        dist = g(row, "dist")
        if dist not in dist_index:
            dist_index[dist] = len(dist_index)
        remark = g(row, "remark")
        rooms = g(row, "rooms")
        rec = [
            dist_index[dist], date, t, cat, top, age10, round(ping * 10), round(price_n), unit,
            remark_flags(remark, g(row, "rtype")), pk, g(row, "addr").translate(FULL), floor_txt, tfloor,
            int(rooms) if rooms.isdigit() else -1, remark[:80], g(row, "id"),
        ]
        if kind == "rent":
            rec.append(g(row, "rtype"))
        out.append(rec)
    return out


def seasons(today, years):
    roc = today.year - 1911
    q = (today.month - 1) // 3 + 1
    res = []
    y, s = roc - years, 1
    while (y, s) <= (roc, q):
        res.append(f"{y}S{s}")
        s += 1
        if s > 4:
            y, s = y + 1, 1
    return res


def main():
    today = dt.date.today()
    min_d = today.replace(year=today.year - YEARS)
    min_date = min_d.year * 10000 + min_d.month * 100 + 1
    os.makedirs(OUT, exist_ok=True)

    sources = [(s, f"{BASE}/DownloadSeason?season={s}&type=zip&fileName=lvr_landcsv.zip") for s in seasons(today, YEARS)]
    sources.append(("本期", f"{BASE}/Download?type=zip&fileName=lvr_landcsv.zip"))

    data = {code: {"sale": {}, "rent": {}, "dist": {}} for code in CITIES}
    got = []
    for label, url in sources:
        log(f"下載 {label}：{url}")
        zf = get_zip(url)
        if not zf:
            continue
        got.append(label)
        for code in CITIES:
            for kind, suffix in (("sale", "a"), ("rent", "c")):
                header, body = read_csv(zf, f"{code}_lvr_land_{suffix}.csv")
                if not header:
                    continue
                recs = convert(header, body, kind, data[code]["dist"], min_date)
                if code == "a" and label == "本期":  # 方便在執行紀錄裡檢查欄位對不對
                    log(f"  [{kind}] 欄名：{header}")
                    for r in body[:2]:
                        log(f"  [{kind}] 原始：{r}")
                    for r in recs[:3]:
                        log(f"  [{kind}] 整理後：{r}")
                    from collections import Counter
                    log(f"  [{kind}] 型態×樓別：{sorted(Counter((r[2], r[3]) for r in recs).items())}")
                    log(f"  [{kind}] 頂層 {sum(r[4] for r in recs)}、特殊 {sum(r[9] & 1 for r in recs)}、增建 {sum(r[9] & 2 > 0 for r in recs)}、有備註 {sum(r[9] & 4 > 0 for r in recs)}、車位未拆價 {sum(r[10] == 2 for r in recs)}")
                    log(f"  [{kind}] 備註樣本：{[r[15] for r in recs if r[15]][:25]}")
                store = data[code][kind]
                for r in recs:
                    key = r[16] or f"{r[11]}|{r[1]}|{r[7]}"
                    store[key] = r
        log(f"  完成 {label}，目前買賣 {sum(len(d['sale']) for d in data.values())} 筆、租賃 {sum(len(d['rent']) for d in data.values())} 筆")

    if not got:
        log("一個資料檔都沒下載到，停止。")
        sys.exit(1)

    index = {"updated": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
             "from": min_date, "sources": got, "cities": []}
    for code, name in CITIES.items():
        d = data[code]
        dists = sorted(d["dist"], key=lambda k: d["dist"][k])
        counts = {}
        for kind, fields in (("sale", SALE_FIELDS), ("rent", RENT_FIELDS)):
            rows = sorted(d[kind].values(), key=lambda r: r[1])
            for r in rows:
                r.pop(16)  # 編號只拿來去重
            counts[kind] = len(rows)
            # 先 gzip 壓縮（約小 5 倍），網頁下載後在瀏覽器裡解壓
            payload = json.dumps({"city": name, "fields": [f for f in fields if f != "編號"], "districts": dists, "rows": rows},
                                 ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            with open(os.path.join(OUT, f"{code.upper()}_{kind}.json.gz"), "wb") as fh:
                fh.write(gzip.compress(payload, 9, mtime=0))
        index["cities"].append({"code": code.upper(), "name": name, "sale": counts["sale"], "rent": counts["rent"]})
        log(f"{name}：買賣 {counts['sale']}、租賃 {counts['rent']}")
    with open(os.path.join(OUT, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
