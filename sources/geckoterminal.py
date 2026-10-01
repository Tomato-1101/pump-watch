"""GeckoTerminal（鍵不要）。新規プールと trending の「発見」に使う。

実効上限は公称 30/分よりずっと低い（連投すると 5〜15 コールで 429 になり、その後
70 秒待っても 5 コールで再び 429 になる）。呼び出し側が分単位の予算（既定 8/分）と
429 後のブロック（既定 90 秒）で守る。ここでは 1 コール分の取得と整形だけを担う。

bsc の new_pools には four-meme のボンディングカーブ新規プールが出るので、
BSC の「打ち上げ直後」の発見源になる。
"""
import calendar
import time

import rules

from . import http

BASE = "https://api.geckoterminal.com/api/v2"
HEADERS = {"Accept": "application/json;version=20230302"}


def _ts(v):
    """pool_created_at は ISO8601 文字列（稀に epoch）なので両方受ける。"""
    n = rules.num(v)
    if n is not None:
        return n
    if not v:
        return None
    try:
        return calendar.timegm(time.strptime(str(v).replace("Z", "GMT"), "%Y-%m-%dT%H:%M:%S%Z"))
    except ValueError:
        return None


def _addr(chain, pool):
    """relationships.base_token.data.id は "bsc_0x…" 形式。"""
    ref = ((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}
    ident = str(ref.get("id") or "")
    addr = ident.split("_", 1)[1] if "_" in ident else ""
    return addr if chain == "sol" else addr.lower()


def _row(chain, pool):
    a = pool.get("attributes") or {}
    addr = _addr(chain, pool)
    if not addr:
        return None
    mcap = rules.num(a.get("market_cap_usd"))
    fdv = rules.num(a.get("fdv_usd"))
    vol = a.get("volume_usd") or {}
    tx5 = (a.get("transactions") or {}).get("m5") or {}
    pc = a.get("price_change_percentage") or {}
    dex = ((pool.get("relationships") or {}).get("dex") or {}).get("data") or {}
    row = {k: None for k in rules.ROW_KEYS}
    row.update({
        "chain": chain, "address": addr, "source": "gt",
        # name は "SYM / QUOTE" なので左側だけをシンボルとして使う
        "symbol": str(a.get("name") or "").split("/")[0].strip() or None,
        "name": a.get("name"),
        "price": rules.num(a.get("base_token_price_usd")),
        "mcap": mcap if mcap is not None else fdv,
        "mcap_is_fdv": mcap is None and fdv is not None,
        "liquidity": rules.num(a.get("reserve_in_usd")),
        "vol5m": rules.num(vol.get("m5")), "vol24h": rules.num(vol.get("h24")),
        "buys5m": rules.num(tx5.get("buys")), "sells5m": rules.num(tx5.get("sells")),
        "buyers5m": rules.num(tx5.get("buyers")),
        "pc5m": rules.num(pc.get("m5")), "pc1h": rules.num(pc.get("h1")),
        "pc24h": rules.num(pc.get("h24")),
        "created_ts": _ts(a.get("pool_created_at")),
        "dex": dex.get("id"),
    })
    return row


def _fetch(chain, path):
    data, status = http.get_json(f"{BASE}/networks/{chain}/{path}", HEADERS)
    pools = (data or {}).get("data") if isinstance(data, dict) else None
    if not isinstance(pools, list):
        return [], status
    return [r for r in (_row(chain, p) for p in pools if isinstance(p, dict)) if r], status


def trending(chain, duration="5m"):
    return _fetch(chain, f"trending_pools?duration={duration}&page=1")


def new_pools(chain):
    return _fetch(chain, "new_pools?page=1")


def top_volume(chain):
    return _fetch(chain, "pools?sort=h24_volume_usd_desc&page=1")


FETCHERS = {"trending": trending, "new": new_pools, "h24": top_volume}
