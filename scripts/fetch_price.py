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

# 建物型態 → 代碼（其餘型態如辦公、工廠不收）
TYPES = [("公寓", 1), ("華廈", 2), ("住宅大樓", 3), ("套房", 4), ("透天厝", 5), ("店面", 6)]

SPECIAL_WORDS = [
    "親友", "員工", "特殊關係", "二親等", "親屬", "共有人", "急買急賣", "急售", "急買", "瑕疵", "凶宅", "非自然死亡",
    "政府機關", "法拍", "拍賣", "債權", "債務", "抵債", "地上權", "畸零地", "調處", "公共設施保留地",
    "民情風俗", "交換", "合併", "持分", "受贈", "贈與", "租約", "含租約", "附帶租約", "道路用地", "法院",
]
MEZZ_WORDS = ["夾層"]  # 備註旗標 2：含夾層

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
    if body and body[0] and not any("\u4e00" <= ch <= "\u9fff" for ch in "".join(body[0])):  # 第二列是英文欄名
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
        if any(w in remark for w in MEZZ_WORDS):
            f |= 2
    return f


CN_DIGIT = "〇一二三四五六七八九"


def zone_name(urban, nonurban):
    """都市土地使用分區 → 簡短名稱，例如 住三、住三之一、商二、住宅區、非都市：鄉村區。"""
    t = (urban or "").replace("都市：", "").strip("。 ")
    if t.startswith("其他:") or t.startswith("其他："):
        t = t[3:]
    if not t:
        n = (nonurban or "").strip("。 ")
        return f"非都市：{n}" if n else "未登記"
    t = t.replace("參", "三").replace("肆", "四").replace("貳", "二").replace("壹", "一")
    m = re.search(r"第([一二三四五六七八九\d])種?(之[一二三\d]|[-－]\d)?種?(住宅|商業|工業)區", t)
    if m:
        n = m.group(1)
        n = CN_DIGIT[int(n)] if n.isdigit() else n
        sub = m.group(2) or ""
        if sub and sub[0] in "-－":
            sub = "之" + sub[1:]
        if sub and sub[1:].isdigit():
            sub = "之" + CN_DIGIT[int(sub[1:])]
        return f"{m.group(3)[0]}{n}{sub}"
    simple = {"住": "住宅區", "商": "商業區", "工": "工業區", "農": "農業區", "其他": "其他"}
    if t in simple:
        return simple[t]
    for k in ("住宅區", "商業區", "工業區", "農業區", "保護區", "風景區", "文教區", "機關用地", "學校用地", "公園用地", "道路用地", "市場用地", "特定專用區", "河川區", "保存區"):
        if k in t:
            return k
    return re.split(r"[(（，。、]", t)[0][:10] or "其他"


def cn_digits(s):
    return re.sub(r"([〇零一二兩三四五六七八九十百]+)(巷|弄|號|之)", lambda m: str(cn_num(m.group(1).replace("兩", "二").replace("零", "")) or m.group(1)) + m.group(2), s)


def geo_key(addr, dist):
    """把地址歸到「路段＋巷」或「路段＋門牌（每 20 號一組）」，網頁拿來查座標，地址太散時才不會查太多次。"""
    a = re.sub(r"\s+", "", addr)
    i = a.find(dist) if dist else -1
    if i >= 0:
        a = a[i + len(dist):]
    a = re.sub(r"^.{1,4}?[里村]", "", a) if re.match(r"^.{1,4}?[里村](.+[路街道])", a) else a
    a = re.sub(r"^\d+鄰", "", a)
    a = cn_digits(a)
    m = re.match(r"(.+?(?:路|街|大道|道)(?:[一二三四五六七八九十]+段)?)(.*)$", a)
    if not m:
        return ""
    road, rest = m.group(1), m.group(2)
    lane = re.match(r"(\d+)巷", rest)
    if lane:
        return f"{road}{lane.group(1)}巷"
    num = re.match(r"(\d+)(?:[~～\-至](\d+))?號", rest)
    if num:
        a1 = int(num.group(1))
        a2 = int(num.group(2) or a1)
        mid = (a1 + a2) // 2
        b = max(1, round(mid / 20) * 20) | (a1 & 1)  # 保留單雙號（路的兩側）
        return f"{road}{b}號"
    return road


PK_TYPES = ["坡道平面", "坡道機械", "升降平面", "升降機械", "塔式車位", "一樓平面", "其他"]


def pk_type(t):
    t = (t or "").strip()
    return PK_TYPES.index(t) if t in PK_TYPES else len(PK_TYPES) - 1


def type_code(t):
    for k, v in TYPES:
        if t.startswith(k):
            return v
    return 0


def convert_park(header, body, bases, dist_index):
    """車位：優先用 _a_park.csv 每個車位的價格；沒有這個檔時用主檔的車位總價 ÷ 車位數。"""
    out = []
    if header:
        c = Cols(header)
        I = {"id": c.idx("編號"), "type": c.idx("車位類別"), "price": c.idx("車位價格", "車位總價", "車位價"),
             "area": c.idx("車位面積平方公尺", "車位面積"), "floor": c.idx("車位所在樓層", "車位樓層", "樓層")}
        if I["id"] < 0 or I["price"] < 0:
            log(f"  車位檔欄位不認得：{header}")
            header = None
        else:
            for row in body:
                g = lambda k: row[I[k]].strip() if 0 <= I[k] < len(row) else ""
                b = bases.get(g("id"))
                price = num(g("price"))
                if not b or price <= 0:
                    continue
                out.append(park_rec(b["rec"], g("type"), price, num(g("area")), g("floor")))
    if not header:
        for b in bases.values():
            if b["pkprice"] > 0 and b["npk"] >= 1:
                n = b["npk"]
                for _ in range(n):
                    out.append(park_rec(b["rec"], b["pktype"], b["pkprice"] / n, b["pkarea"] / n, ""))
    return out


def park_rec(rec, ptype, price, area_m2, floor):
    # 欄位跟買賣一樣排，網頁可以共用篩選：坪數＝車位面積、總價＝單價＝每個車位價格、車位欄＝車位類別
    return [rec[0], rec[1], rec[2], 0, 0, rec[5], round(area_m2 * 0.3025 * 10), round(price), round(price), rec[9],
            pk_type(ptype), rec[11], floor, rec[13], -1, rec[15], rec[16], rec[17], rec[18]]


SALE_FIELDS = ["區", "日期", "型態", "樓別", "頂層", "屋齡", "坪數", "總價", "單價", "備註旗標", "車位", "地址", "移轉層次", "總樓層", "房", "備註", "編號", "使用分區", "座標組"]
RENT_FIELDS = SALE_FIELDS + ["出租型態"]
PARK_FIELDS = ["區", "日期", "型態", "樓別", "頂層", "屋齡", "車位坪數", "車位價格", "車位價格", "備註旗標", "車位類別", "地址", "車位樓層", "總樓層", "房", "備註", "編號", "使用分區", "座標組"]


def convert(header, body, kind, dist_index, min_date, bases=None):
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
        "zone_u": c.idx("都市土地使用分區"),
        "zone_n": c.idx("非都市土地使用分區"),
        "deal": c.idx("交易筆棟數", "租賃筆棟數"),
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
            zone_name(g(row, "zone_u"), g(row, "zone_n")), geo_key(rec_addr := g(row, "addr").translate(FULL), dist),
        ]
        if bases is not None and rec[16]:
            npk = re.search(r"車位(\d+)", g(row, "deal"))
            bases[rec[16]] = {"rec": rec, "pktype": g(row, "pktype"), "pkprice": pk_price, "pkarea": pk_area, "npk": int(npk.group(1)) if npk else 0}
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

    data = {code: {"sale": {}, "rent": {}, "park": {}, "dist": {}} for code in CITIES}
    got = []
    for label, url in sources:
        log(f"下載 {label}：{url}")
        zf = get_zip(url)
        if not zf:
            continue
        got.append(label)
        if label == "本期":
            log(f"  壓縮檔內容（臺北市）：{[n for n in zf.namelist() if n.lower().startswith('a_')]}")
        for code in CITIES:
            bases = {}
            for kind, suffix in (("sale", "a"), ("rent", "c")):
                header, body = read_csv(zf, f"{code}_lvr_land_{suffix}.csv")
                if not header:
                    continue
                recs = convert(header, body, kind, data[code]["dist"], min_date, bases if kind == "sale" else None)
                if code == "a" and label == "本期":  # 方便在執行紀錄裡檢查欄位對不對
                    log(f"  [{kind}] 欄名：{header}")
                    for r in body[:2]:
                        log(f"  [{kind}] 原始：{r}")
                    for r in recs[:3]:
                        log(f"  [{kind}] 整理後：{r}")
                    from collections import Counter
                    from collections import Counter
                    log(f"  [{kind}] 原始型態：{Counter(r[header.index('建物型態')] for r in body if len(r) > 12).most_common(20)}；交易標的：{Counter(r[1] for r in body if len(r) > 2).most_common(10)}")
                    log(f"  [{kind}] 型態×樓別：{sorted(Counter((r[2], r[3]) for r in recs).items())}")
                    log(f"  [{kind}] 頂層 {sum(r[4] for r in recs)}、特殊 {sum(r[9] & 1 for r in recs)}、夾層 {sum(r[9] & 2 > 0 for r in recs)}、有備註 {sum(r[9] & 4 > 0 for r in recs)}、車位未拆價 {sum(r[10] == 2 for r in recs)}")
                    log(f"  [{kind}] 備註樣本：{[r[15] for r in recs if r[15]][:25]}")
                store = data[code][kind]
                for r in recs:
                    key = r[16] or f"{r[11]}|{r[1]}|{r[7]}"
                    store[key] = r
            ph, pb = read_csv(zf, f"{code}_lvr_land_a_park.csv")
            precs = convert_park(ph, pb, bases, data[code]["dist"])
            if code == "a" and label == "本期":
                log(f"  [park] 欄名：{ph}")
                for r in pb[:3]:
                    log(f"  [park] 原始：{r}")
                for r in precs[:3]:
                    log(f"  [park] 整理後：{r}")
                from collections import Counter
                log(f"  [park] 類別：{sorted(Counter(r[10] for r in precs).items())}；使用分區（買賣）：{Counter(b['rec'][17] for b in bases.values()).most_common(15)}")
                log(f"  [geo] 樣本：{[(b['rec'][11], b['rec'][18]) for b in list(bases.values())[:15]]}")
                log(f"  [geo] 沒有座標組：{[b['rec'][11] for b in bases.values() if not b['rec'][18]][:15]}")
            pstore = data[code]["park"]
            seen = {}
            for r in precs:  # 同一筆交易的第幾個車位，跨季檔案重複出現時會覆蓋而不是重複
                k = seen[r[16]] = seen.get(r[16], -1) + 1
                pstore[f"{r[16]}#{k}"] = r
        log(f"  完成 {label}，目前買賣 {sum(len(d['sale']) for d in data.values())} 筆、租賃 {sum(len(d['rent']) for d in data.values())} 筆、車位 {sum(len(d['park']) for d in data.values())} 個")

    if not got:
        log("一個資料檔都沒下載到，停止。")
        sys.exit(1)

    index = {"updated": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
             "from": min_date, "sources": got, "cities": []}
    for code, name in CITIES.items():
        d = data[code]
        dists = sorted(d["dist"], key=lambda k: d["dist"][k])
        counts = {}
        for kind, fields in (("sale", SALE_FIELDS), ("rent", RENT_FIELDS), ("park", PARK_FIELDS)):
            rows = sorted(d[kind].values(), key=lambda r: r[1])
            zones, geos = {}, {}
            for r in rows:
                r.pop(16)  # 編號只拿來去重
                # 使用分區、座標組改成編號，檔案小很多
                r[16] = zones.setdefault(r[16], len(zones))
                r[17] = geos.setdefault(r[17], len(geos)) if r[17] else -1
            counts[kind] = len(rows)
            # 先 gzip 壓縮（約小 5 倍），網頁下載後在瀏覽器裡解壓
            payload = json.dumps({"city": name, "fields": [f for f in fields if f != "編號"], "districts": dists,
                                  "zones": list(zones), "geokeys": list(geos), "pkTypes": PK_TYPES, "rows": rows},
                                 ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            with open(os.path.join(OUT, f"{code.upper()}_{kind}.json.gz"), "wb") as fh:
                fh.write(gzip.compress(payload, 9, mtime=0))
        index["cities"].append({"code": code.upper(), "name": name, "sale": counts["sale"], "rent": counts["rent"], "park": counts["park"]})
        log(f"{name}：買賣 {counts['sale']}、租賃 {counts['rent']}、車位 {counts['park']}")
    with open(os.path.join(OUT, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
