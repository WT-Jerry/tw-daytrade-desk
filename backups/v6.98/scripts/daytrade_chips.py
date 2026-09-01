#!/usr/bin/env python3
"""三大法人（TWSE T86）+ 券商分點（Fubon DJ 可指定歷史日）。"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from typing import Any, Dict, List, Optional


def http_get(url: str, timeout: int, ua: str, retries: int = 3) -> str:
    last_err: Optional[Exception] = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": ua, "Accept": "application/json,text/plain,*/*"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8-sig", errors="replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last_err}")


def to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    s = str(x).strip().replace(",", "").replace("%", "").replace("元", "")
    if s in {"", "-", "--", "---", "null", "None", "X", "x"}:
        return None
    s = s.replace("+", "")
    try:
        return float(s)
    except ValueError:
        return None


def to_int(x: Any) -> Optional[int]:
    f = to_float(x)
    if f is None:
        return None
    return int(round(f))

T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86?date={date}&selectType=ALL&response=json"
BROKER_URL = (
    "https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco.djhtm"
    "?a={code}&c=E&e={d1}&f={d2}"
)

# 常見隔日沖／外資當沖常客（名稱包含即可）
DAYTRADE_BROKER_HINTS = (
    "美林",
    "高盛",
    "摩根",
    "瑞銀",
    "野村",
    "麥格理",
    "新加坡",
    "港商",
    "台灣摩根",
)


def _ymd_slash(ymd: str) -> str:
    return f"{ymd[:4]}/{ymd[4:6]}/{ymd[6:8]}"


def fetch_t86(date_ymd: str, timeout: int, ua: str) -> Dict[str, Dict[str, Any]]:
    """全市場三大法人買賣超（股）。失敗則丟例外由呼叫端略過。"""
    raw = http_get(T86_URL.format(date=date_ymd), timeout=max(timeout, 60), ua=ua)
    payload = json.loads(raw)
    if payload.get("stat") != "OK":
        raise RuntimeError(f"T86 stat={payload.get('stat')}")
    fields = [str(x) for x in (payload.get("fields") or [])]
    idx = {n: i for i, n in enumerate(fields)}
    need = "證券代號"
    if need not in idx:
        raise RuntimeError("T86 missing 證券代號")

    def col(*names: str) -> Optional[int]:
        for n in names:
            if n in idx:
                return idx[n]
            for k, i in idx.items():
                if n in k:
                    return i
        return None

    i_code = idx["證券代號"]
    i_name = col("證券名稱")
    i_foreign = col("外陸資買賣超股數(不含外資自營商)")
    i_trust = col("投信買賣超股數")
    i_dealer = col("自營商買賣超股數")
    i_all = col("三大法人買賣超股數")

    out: Dict[str, Dict[str, Any]] = {}
    for row in payload.get("data") or []:
        if not row or i_code >= len(row):
            continue
        code = str(row[i_code]).strip()
        if not re.match(r"^\d{4}$", code):
            continue
        out[code] = {
            "name": str(row[i_name]).strip() if i_name is not None and i_name < len(row) else "",
            "foreign_net_shares": to_int(row[i_foreign]) if i_foreign is not None and i_foreign < len(row) else None,
            "trust_net_shares": to_int(row[i_trust]) if i_trust is not None and i_trust < len(row) else None,
            "dealer_net_shares": to_int(row[i_dealer]) if i_dealer is not None and i_dealer < len(row) else None,
            "inst_net_shares": to_int(row[i_all]) if i_all is not None and i_all < len(row) else None,
        }
    return out


def _cells_of_row(row_html: str) -> List[str]:
    cells = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", row_html, flags=re.I)
    out = []
    for c in cells:
        t = re.sub(r"<[^>]+>", "", c)
        t = " ".join(t.split())
        if t:
            out.append(t)
    return out


def parse_broker_html(text: str) -> Dict[str, Any]:
    date_m = re.search(r"最後更新日[:：]\s*([0-9]{4}/[0-9]{1,2}/[0-9]{1,2})", text)
    page_date = None
    if date_m:
        y, m, d = date_m.group(1).split("/")
        page_date = f"{int(y):04d}{int(m):02d}{int(d):02d}"

    buys: List[Dict[str, Any]] = []
    sells: List[Dict[str, Any]] = []
    for row in re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", text, flags=re.I):
        cells = _cells_of_row(row)
        if len(cells) < 8:
            continue
        if cells[0] in {"買超", "買超券商"} or "合計" in cells[0] or "平均" in cells[0]:
            continue
        if not re.search(r"\d", cells[1] if len(cells) > 1 else ""):
            continue
        # left: 買超券商 買進 賣出 買超 佔成交比重
        # right: 賣超券商 買進 賣出 賣超 佔成交比重
        buy_name, buy_in, buy_out, buy_net, buy_w = cells[0], cells[1], cells[2], cells[3], cells[4]
        sell_name, sell_in, sell_out, sell_net, sell_w = cells[5], cells[6], cells[7], cells[8], cells[9] if len(cells) > 9 else ""
        bn = to_float(buy_net)
        if buy_name and bn is not None:
            buys.append(
                {
                    "name": buy_name,
                    "net_lots": bn,
                    "weight": (to_float(buy_w) or 0) / 100.0 if buy_w else None,
                    "buy_lots": to_float(buy_in),
                    "sell_lots": to_float(buy_out),
                }
            )
        sn = to_float(sell_net)
        if sell_name and sn is not None:
            sells.append(
                {
                    "name": sell_name,
                    "net_lots": sn,
                    "weight": (to_float(sell_w) or 0) / 100.0 if sell_w else None,
                    "buy_lots": to_float(sell_in),
                    "sell_lots": to_float(sell_out),
                }
            )

    def top5_sum(items: List[Dict[str, Any]]) -> Dict[str, Any]:
        top = items[:5]
        lots = sum(float(x.get("net_lots") or 0) for x in top)
        wts = [x.get("weight") for x in top if x.get("weight") is not None]
        return {
            "lots": lots,
            "ratio": float(sum(wts)) if wts else None,
            "names": [x["name"] for x in top],
            "top": top,
        }

    return {
        "page_date": page_date,
        "buy": top5_sum(buys),
        "sell": top5_sum(sells),
    }


def fetch_broker_branch(code: str, date_ymd: str, timeout: int, ua: str) -> Optional[Dict[str, Any]]:
    slash = _ymd_slash(date_ymd)
    url = BROKER_URL.format(code=code, d1=slash, d2=slash)
    # Fubon pages are big5
    last_err = None
    for i in range(3):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": ua or "Mozilla/5.0", "Accept": "text/html,*/*"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            text = raw.decode("big5", errors="replace")
            parsed = parse_broker_html(text)
            if parsed.get("page_date") and parsed["page_date"] != date_ymd:
                parsed["date_mismatch"] = True
            return parsed
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.8 * (i + 1))
    return {"error": str(last_err)}


def is_daytrade_broker(name: str) -> bool:
    return any(h in (name or "") for h in DAYTRADE_BROKER_HINTS)
