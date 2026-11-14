"""
每日抓取台股與美股資料，輸出 data.json 給 index.html 使用。
只用 Python 內建模組，不需要安裝任何套件。
資料來源：
  - 臺灣證券交易所（加權指數收盤、成交金額、漲跌家數、三大法人）
  - Yahoo Finance（盤中高低、櫃買指數、新台幣、美股、費半、台積電 ADR）
任何一項抓不到時，該欄位留空（null），網頁會顯示「—」，其他資料照常更新。
"""
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TPE = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/json,text/plain,*/*"}
errors = []


def get_json(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url} -> {last}")


def num(s):
    """'1,234.56' / '+12.3' / '817(25)' / '--' -> float 或 None"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).replace(",", "").strip()
    if "(" in s:
        s = s.split("(")[0]
    s = s.replace("+", "")
    try:
        return float(s)
    except ValueError:
        return None


def roc_to_iso(s):
    """'115/11/13' -> '2026-11-13'"""
    y, m, d = s.strip().split("/")
    return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"


# ---------------- 證交所 ----------------
def twse_daily_rows():
    """FMTQIK：每月每日的成交金額與加權指數收盤。抓本月和上月，回傳依日期排序的列表。"""
    today = datetime.now(TPE)
    first = today.replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    rows = []
    for d in (prev, first):
        url = ("https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"
               f"?date={d:%Y%m%d}&response=json")
        j = get_json(url)
        if j.get("stat") != "OK":
            continue
        f = j["fields"]
        i_date, i_amt, i_idx, i_chg = (f.index("日期"), f.index("成交金額"),
                                       f.index("發行量加權股價指數"), f.index("漲跌點數"))
        for r in j["data"]:
            rows.append({"date": roc_to_iso(r[i_date]),
                         "turnover": num(r[i_amt]),
                         "close": num(r[i_idx]),
                         "change": num(r[i_chg])})
        time.sleep(2)
    rows.sort(key=lambda x: x["date"])
    return rows


def twse_breadth(date_compact):
    url = ("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
           f"?date={date_compact}&type=MS&response=json")
    j = get_json(url)
    for t in j.get("tables", []):
        if "漲跌證券數" in (t.get("title") or ""):
            f = t["fields"]
            col = f.index("股票") if "股票" in f else len(f) - 1
            out = {}
            for r in t["data"]:
                k = r[0]
                if k.startswith("上漲"):
                    out["up"] = num(r[col])
                elif k.startswith("下跌"):
                    out["down"] = num(r[col])
                elif k.startswith("持平"):
                    out["flat"] = num(r[col])
            if out:
                return out
    raise RuntimeError("MI_INDEX 找不到漲跌證券數表")


def twse_institutional(date_compact):
    url = ("https://www.twse.com.tw/rwd/zh/fund/BFI82U"
           f"?type=day&dayDate={date_compact}&response=json")
    j = get_json(url)
    if j.get("stat") != "OK":
        raise RuntimeError(f"BFI82U stat={j.get('stat')}")
    f = j["fields"]
    i_net = f.index("買賣差額")
    foreign = trust = dealer = total = 0.0
    for r in j["data"]:
        name, v = r[0], (num(r[i_net]) or 0) / 1e8  # 元 -> 億元
        if "外資" in name:
            foreign += v
        elif name.startswith("投信"):
            trust += v
        elif name.startswith("自營商"):
            dealer += v
        elif name.startswith("合計"):
            total = v
    return {"foreign": round(foreign, 2), "trust": round(trust, 2),
            "dealer": round(dealer, 2), "total": round(total, 2)}


# ---------------- Yahoo ----------------
def yahoo(symbol):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           "?range=1mo&interval=1d")
    j = get_json(url)
    res = j["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    pts = []
    for i, ts in enumerate(res.get("timestamp") or []):
        c = q["close"][i]
        if c is None:
            continue
        pts.append({"date": datetime.fromtimestamp(ts, TPE).strftime("%Y-%m-%d"),
                    "open": q["open"][i], "high": q["high"][i],
                    "low": q["low"][i], "close": c})
    if len(pts) < 2:
        raise RuntimeError(f"{symbol} 資料不足")
    last, prev = pts[-1], pts[-2]
    return {"date": last["date"], "open": last["open"], "high": last["high"],
            "low": last["low"], "close": last["close"], "prev": prev["close"],
            "change": last["close"] - prev["close"],
            "pct": (last["close"] / prev["close"] - 1) * 100}


def safe(label, fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # noqa
        errors.append(f"{label}: {e}")
        print("!!", label, e)
        return None


def main():
    out = {"updated": datetime.now(TPE).strftime("%Y-%m-%d %H:%M"), "tw": {}, "us": {}}

    rows = safe("證交所每日收盤", twse_daily_rows) or []
    if rows:
        last = rows[-1]
        prev = rows[-2] if len(rows) > 1 else None
        out["tw"]["date"] = last["date"]
        out["tw"]["taiex"] = {
            "close": last["close"], "change": last["change"],
            "prev": prev["close"] if prev else None,
            "pct": (last["change"] / prev["close"] * 100) if prev and prev["close"] else None,
            "turnover": last["turnover"] / 1e8 if last["turnover"] else None,  # 億元
        }
        out["tw"]["trend"] = [{"date": r["date"], "close": r["close"]} for r in rows[-10:]]
        dc = last["date"].replace("-", "")
        time.sleep(2)
        out["tw"]["breadth"] = safe("漲跌家數", twse_breadth, dc)
        time.sleep(2)
        out["tw"]["inst"] = safe("三大法人", twse_institutional, dc)

    tw = safe("Yahoo 加權指數", yahoo, "^TWII")
    if tw:
        t = out["tw"].setdefault("taiex", {})
        for k in ("open", "high", "low"):
            t[k] = tw[k]
        # 若證交所沒抓到，用 Yahoo 補
        if t.get("close") is None:
            t.update({"close": tw["close"], "change": tw["change"], "prev": tw["prev"], "pct": tw["pct"]})
            out["tw"].setdefault("date", tw["date"])
    out["tw"]["otc"] = safe("Yahoo 櫃買指數", yahoo, "^TWOII")
    out["tw"]["twd"] = safe("Yahoo 新台幣", yahoo, "TWD=X")

    for key, sym in (("sp500", "^GSPC"), ("nasdaq", "^IXIC"), ("dow", "^DJI"),
                     ("sox", "^SOX"), ("tsm", "TSM")):
        out["us"][key] = safe(f"Yahoo {sym}", yahoo, sym)
        time.sleep(1)

    out["errors"] = errors
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("done; errors:", errors)


if __name__ == "__main__":
    main()
