"""EMS の平常値（全体フロアと同齢コホート）を `chain_buckets` から作る。

「30 秒で買い手 3 人」が急増かどうかは、その銘柄の平常値が無いと決まらない。だが実測では
初点火の 48% は直前 15 分の中央値が 0 で、比も z も定義できない。そこで
  - 全体フロア: チェーン全体の分次 p50（買い手/分・出来高/分）を μ と中央値の下限にする
  - 同齢コホート: 生後 60 分未満の銘柄は、発射台 × 生後分の p50 を平常値に使う
の 2 段で埋める。

**偏りの注意**: 分次バケットは「取引のあった分」しか存在しない。ここで出す p50 は
「動いている銘柄の動いている分」の中央値で、沈黙している分を 0 として含んでいない。
つまり実際の平常値より高めに出る＝発火しにくい側に倒れる。これは承知のうえで採っている
（沈黙分を 0 で埋めると p50 が全部 0 になり、フロアだけが効く＝絶対閾値に戻ってしまう）。

計算は起動時と 6 時間ごとだけ（`refresh`）。結果は `kv` に JSON で置き、再起動しても
即座に使える。
"""
import json
import statistics
import time

HARD_FLOOR_BUYERS = 0.5      # 人/分。これ未満には下げない（μ→0 で G が発散するのを防ぐ）
HARD_FLOOR_VOL = 100.0       # USD/分。同上（中央値 0 の銘柄が全部満点になるのを防ぐ）
COHORT_MAX_MIN = 60          # 生後この分までをコホートで見る
COHORT_MIN_SAMPLES = 5       # この件数に満たないセルは採らない（1 銘柄の癖を平常値にしない）
MAX_AGE = 6 * 3600           # 再計算の間隔（秒）
KV_PREFIX = "score_baseline_"


def compute(conn, chain, now=None, hours=24):
    """直近 hours 時間のバケットから全体フロアと同齢コホート表を作る。"""
    now = int(now or time.time())
    since = int(now // 60) - int(hours * 60)
    buyers, vols = [], []
    for r in conn.execute(
            "SELECT buyers,vol_usd FROM chain_buckets WHERE chain=? AND minute>=?"
            " AND (COALESCE(buys,0)+COALESCE(sells,0))>0", (chain, since)):
        buyers.append(float(r["buyers"] or 0))
        vols.append(float(r["vol_usd"] or 0.0))
    floor = {
        "buyers": max(HARD_FLOOR_BUYERS, statistics.median(buyers) if buyers else 0.0),
        "vol": max(HARD_FLOOR_VOL, statistics.median(vols) if vols else 0.0),
    }
    cells = {}
    for r in conn.execute(
            "SELECT t.launchpad AS lp, t.created_ts AS cts, b.minute AS m,"
            " b.buyers AS u, b.vol_usd AS v FROM chain_buckets b"
            " JOIN chain_tokens t ON t.chain=b.chain AND t.address=b.address"
            " WHERE b.chain=? AND b.minute>=? AND t.created_ts IS NOT NULL"
            " AND t.launchpad IS NOT NULL", (chain, since)):
        age = int(r["m"]) - int(r["cts"]) // 60
        if not 0 <= age < COHORT_MAX_MIN:
            continue
        cell = cells.setdefault(r["lp"], {}).setdefault(age, ([], []))
        cell[0].append(float(r["u"] or 0))
        cell[1].append(float(r["v"] or 0.0))
    cohort, n_cells = {}, 0
    for lp, ages in cells.items():
        for age, (us, vs) in ages.items():
            if len(us) < COHORT_MIN_SAMPLES:
                continue
            cohort.setdefault(lp, {})[str(age)] = [statistics.median(us),
                                                   statistics.median(vs)]
            n_cells += 1
    return {"ts": now, "chain": chain, "buckets": len(buyers), "floor": floor,
            "cohort": cohort, "cells": n_cells}


def save(conn, chain, data):
    conn.execute(
        "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key)"
        " DO UPDATE SET value=excluded.value",
        (KV_PREFIX + chain, json.dumps(data, ensure_ascii=False)))


def load(conn, chain):
    r = conn.execute("SELECT value FROM kv WHERE key=?", (KV_PREFIX + chain,)).fetchone()
    if not r or not r["value"]:
        return None
    try:
        return json.loads(r["value"])
    except ValueError:
        return None


def refresh(conn, chain, now=None, max_age=MAX_AGE):
    """古ければ作り直して保存する。(データ, 作り直したか) を返す。"""
    now = int(now or time.time())
    data = load(conn, chain)
    if data and now - int(data.get("ts") or 0) < max_age:
        return data, False
    data = compute(conn, chain, now)
    save(conn, chain, data)
    conn.commit()
    return data, True


# --- 参照 -------------------------------------------------------------------

def floors(data):
    """(買い手/分, 出来高/分) の下限。データが無ければハード下限だけ。"""
    f = (data or {}).get("floor") or {}
    return (max(HARD_FLOOR_BUYERS, float(f.get("buyers") or 0)),
            max(HARD_FLOOR_VOL, float(f.get("vol") or 0)))


def cohort(data, launchpad, age_min):
    """生後 age_min 分の同齢 p50 (買い手/分, 出来高/分)。無ければ None。"""
    if not data or not launchpad or age_min is None or age_min < 0:
        return None
    table = (data.get("cohort") or {}).get(str(launchpad))
    if not table:
        return None
    row = table.get(str(int(age_min)))
    return (float(row[0]), float(row[1])) if row else None


def summary(data):
    """起動時に 1 行で出す説明（フロアとコホートを何件から作ったか）。"""
    if not data:
        return "baseline なし（フロアはハード下限のみ）"
    b, v = floors(data)
    return (f"baseline buckets={data.get('buckets', 0)} floor={b:.1f}人/分 ${v:.0f}/分"
            f" cohort={data.get('cells', 0)}セル/{len(data.get('cohort') or {})}発射台")
