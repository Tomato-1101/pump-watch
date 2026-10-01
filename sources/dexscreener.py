"""DexScreener（鍵不要・300 コール/分・1 コール 30 アドレスまで）。

既知の銘柄を毎サイクル一括更新する主軸。応答は 30 秒キャッシュなので、これより短い
周期で撃っても値は変わらない。同じトークンに複数ペアが返るときは流動性が最大のものを使う。
実測（2026-09-18）: bsc / robinhood / arc とも 200 で pairs が返る（chainId はそのまま）。
"""
from concurrent.futures import ThreadPoolExecutor

import rules

from . import http

BASE = "https://api.dexscreener.com/tokens/v1"
MAX_ADDRS = 30


def _num(v):
    return rules.num(v)


def _row(chain, pair):
    base = pair.get("baseToken") or {}
    addr = str(base.get("address") or "").strip()
    if not addr:
        return None
    mcap = _num(pair.get("marketCap"))
    fdv = _num(pair.get("fdv"))
    vol = pair.get("volume") or {}
    txns = pair.get("txns") or {}
    pc = pair.get("priceChange") or {}
    m5 = txns.get("m5") or {}
    created = _num(pair.get("pairCreatedAt"))
    row = {k: None for k in rules.ROW_KEYS}
    row.update({
        "chain": chain, "address": addr if chain == "sol" else addr.lower(), "source": "ds",
        "symbol": base.get("symbol"), "name": base.get("name"),
        "price": _num(pair.get("priceUsd")),
        "mcap": mcap if mcap is not None else fdv,
        "mcap_is_fdv": mcap is None and fdv is not None,
        "liquidity": _num((pair.get("liquidity") or {}).get("usd")),
        "vol5m": _num(vol.get("m5")), "vol24h": _num(vol.get("h24")),
        "buys5m": _num(m5.get("buys")), "sells5m": _num(m5.get("sells")),
        "pc5m": _num(pc.get("m5")), "pc1h": _num(pc.get("h1")), "pc24h": _num(pc.get("h24")),
        "created_ts": created / 1000.0 if created else None,   # ペア生成時刻（ミリ秒）
        "dex": pair.get("dexId"),
        # 通知に付ける画像。応答に既に入っているので追加コールは要らない
        # （実測 2026-09-18: 直近 bsc 60 件のうち 18 件で入っていた）
        "logo": (pair.get("info") or {}).get("imageUrl") or None,
    })
    return row


def _best(rows):
    """同じトークンの複数ペアから流動性が最大のものを選ぶ。"""
    best = {}
    for r in rows:
        key = (r["chain"], r["address"])
        cur = best.get(key)
        if cur is None or (r.get("liquidity") or 0) > (cur.get("liquidity") or 0):
            best[key] = r
    return list(best.values())


def fetch(chain, addrs, timeout=12):
    """30 件までのアドレスをまとめて引く。(行リスト, HTTP ステータス)。

    timeout は画像を 1 件だけ引くとき（enrich.fetch_logo_url）に短く切るためにある。
    """
    addrs = [a for a in addrs if a][:MAX_ADDRS]
    if not addrs:
        return [], 200
    data, status = http.get_json(f"{BASE}/{chain}/{','.join(addrs)}", timeout=timeout)
    if not isinstance(data, list):
        return [], status
    rows = [r for r in (_row(chain, p) for p in data if isinstance(p, dict)) if r]
    return rows, status


def fetch_many(by_chain, workers=6):
    """{chain: [addr, ...]} を 30 件ずつ並列に引く。(行リスト, コール数, 429 の有無)。

    銘柄が 1,000 を超えると直列では 1 サイクルに収まらないので少数のスレッドで撃つ
    （DexScreener は 300 コール/分なので、この並列度でも上限には当たらない）。
    """
    jobs = [(chain, batch) for chain, addrs in by_chain.items()
            for batch in http.chunked(addrs, MAX_ADDRS)]
    if not jobs:
        return [], 0, False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda j: fetch(*j), jobs))
    rows = [r for rows_, _ in results for r in rows_]
    limited = any(st == 429 for _, st in results)
    return _best(rows), len(jobs), limited
