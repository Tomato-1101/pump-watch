"""銘柄ごとのローリング窓。取引 1 件ずつを足して rules の正規化行を作る。

窓の持ち方:
  直近 5 分   生の取引（ts, side, usd, trader, price）を deque で保持。買い手の実数と
              5 分の買/売/出来高・5 分前比はここから出す（これが速さの肝）。
  それ以上    1 分バケット（buys/sells/buyers/vol/last_price/high）。1h・24h の比較と
              出来高に使い、SQLite に落として再起動をまたぐ。

保有者数・スマートマネー・24h 出来高のような「チェーンだけでは出せない値」はここでは
None のままにし、run.py が API レーンのスナップショットから補う。
"""
import math
import statistics
import threading
import time

import baseline  # noqa: E402  （chain/__init__.py が親をパスに入れている）

WINDOW = 300          # 生の取引を持つ秒数（5 分窓）
IDLE_DROP = 7200      # この秒数まったく取引が無い銘柄はメモリから落とす

# --- EMS の窓と平常値 -------------------------------------------------------
WINDOWS = (10, 30, 120)   # 超初動 / メイン / 確認（§11.3）
LISTED_WINDOW = 600       # 名簿ウォレットの命中を数える秒数
BASE_CUT = 120            # 平常値から外す直近の秒数（最長窓 120s ぶん切る）
BASE_SHORT_MIN = 15       # 短い平常値の分数
BASE_LONG_MIN = 1440      # 長い平常値の分数（24h）
MAD_FLOOR = 0.5           # log 出来高の MAD の下限（分散 0 の銘柄が満点になるのを防ぐ）
MARKET_MAD_FLOOR = 1.0    # 市場補正の MAD の下限
BUNDLE_BUYERS = 5         # 初見 3 ブロックのどれかにこの人数の別々の買い手が居たら bundle
BUNDLE_BLOCKS = 3
BUNDLE_HOLD = 1800        # bundle の印を維持する秒数
G30_RING = 60             # G_U30 を覚えておく秒数（加速度の 30 秒前を取るため）
FOURMEME_BIRTH = 300      # 作成からこの秒数以内に観測を始めた銘柄だけ累計調達額を出す


def _count(v):
    return len(v) if isinstance(v, set) else int(v or 0)


class Agg:
    def __init__(self, chain="bsc", window=WINDOW):
        self.chain = chain
        self.window = window
        self.trades = {}   # addr -> [(ts, side, usd, trader, price), ...]（古い順）
        self.buckets = {}  # addr -> {minute: [buys, sells, buyers, vol, last, high]}
        self.meta = {}     # addr -> 銘柄の属性と最新値
        self.dirty = set()  # 永続化していないバケットを持つ銘柄
        self.lock = threading.Lock()
        self.trade_count = 0
        self.listed = {}      # 自前名簿 addr(小文字) -> {"name", "handle"}
        self.hits = {}        # addr -> [(ts, wallet, side, usd, price, block), ...]
        self.listed_new = []  # 未保存の名簿ウォレット取引（判定スレッドが DB へ落とす）
        self.market = {}      # minute -> チェーン全体の買い手合計（市場補正の素）
        self.g30 = {}         # addr -> [(ts, G_U30), ...] 加速度の 30 秒前を取るリング
        self._zm = (None, 0.0)  # (minute, z_market) 1 分に 1 回だけ計算する
        self.bnb_usd = None   # four.meme の卒業必要額（18 BNB）を USD にするだけに使う

    def set_listed(self, wallets):
        """自前名簿を差し替える（5 分ごとに再読込するので入れ替え式）。

        wallets は {アドレス: {"name": 名前, "handle": ハンドル or None}}。
        """
        with self.lock:
            self.listed = {str(w).lower(): dict(v or {}) for w, v in wallets.items() if w}

    def listed_info(self):
        """名簿の中身のコピー（アドレス → {"name", "handle"}）。"""
        with self.lock:
            return dict(self.listed)

    def take_listed_trades(self):
        """前回の take 以降に見た名簿ウォレットの取引を取り出す（取ったら空になる）。

        戻りは (wallet, address, ts, side, usd, price, block) の list。
        取り込みスレッドから DB を触らせないため、判定スレッドがこれを引いて書く。
        """
        with self.lock:
            rows, self.listed_new = self.listed_new, []
        return rows

    # --- 取り込み ---------------------------------------------------------

    def add(self, t):
        with self.lock:
            self._add(t)

    def add_many(self, trades):
        with self.lock:
            for t in trades:
                self._add(t)

    def _add(self, t):
        self.trade_count += 1
        m = self.meta.setdefault(t.address, {"first_ts": t.ts})
        for key, val in (("symbol", t.symbol), ("supply", t.supply), ("dex", t.dex),
                         ("launchpad", t.launchpad), ("created_ts", t.created_ts),
                         ("liq", t.liq)):
            if val is not None:
                m[key] = val
        if t.price > 0:
            m["price"] = t.price
        m["last_ts"] = max(t.ts, m.get("last_ts", 0))
        m["last_block"] = t.block
        self.trades.setdefault(t.address, []).append(
            (t.ts, t.side, t.usd, t.trader, t.price))
        minute = int(t.ts // 60)
        b = self.buckets.setdefault(t.address, {}).setdefault(
            minute, [0, 0, set(), 0.0, None, None])
        b[0 if t.side == "buy" else 1] += 1
        if t.side == "buy" and isinstance(b[2], set) and t.trader not in b[2]:
            b[2].add(t.trader)
            # チェーン全体の分次買い手合計（市場補正の素）。ここで数えておかないと、
            # バケットの買い手が件数に畳まれた後は全銘柄ぶん足し直すしかなくなる
            self.market[minute] = self.market.get(minute, 0) + 1
        b[3] += t.usd
        if t.price > 0:
            b[4] = t.price
            b[5] = t.price if b[5] is None else max(b[5], t.price)
        self._ems_add(m, t)
        self.dirty.add(t.address)

    def _ems_add(self, m, t):
        """EMS だけが使う付随情報（名簿の命中・累計調達額・bundle 判定）。

        いずれも O(1)。取り込みは絶対に止められないのでここで重い処理はしない。
        """
        if t.trader in self.listed:
            # 買いだけでなく売りも残す（通知に「売り抜け済み」を出すため）。
            self.hits.setdefault(t.address, []).append(
                (t.ts, t.trader, t.side, t.usd, t.price, t.block))
            self.listed_new.append(
                (t.trader, t.address, t.ts, t.side, t.usd, t.price, t.block))
        # four.meme のカーブ進捗は「初見からの累計 (買い − 売り)」でしか出せない。
        # 作成から離れて観測を始めた銘柄（再起動後など）は端から数えていないので、
        # features 側で raised_ts と作成時刻を突き合わせて捨てる
        if "raised_ts" not in m:
            m["raised_ts"], m["raised"] = t.ts, 0.0
        m["raised"] += t.usd if t.side == "buy" else -t.usd
        blk = m.get("blk")
        if blk is None and not m.get("blk_done"):
            blk = m["blk"] = {}
        if blk is None:
            return
        if t.ts not in blk and len(blk) >= BUNDLE_BLOCKS:
            m.pop("blk", None)                 # 初見 3 ブロックを見終わった
            m["blk_done"] = True
            return
        if t.side != "buy":
            return
        s = blk.setdefault(t.ts, set())
        s.add(t.trader)
        if len(s) >= BUNDLE_BUYERS:
            m["bundle_ts"] = t.ts
            m.pop("blk", None)
            m["blk_done"] = True

    def load_buckets(self, loaded):
        """再起動時に SQLite から読み戻す（{addr: {minute: [...]}}）。

        last_ts も入れる。入れないと「最後の取引が大昔」と見なされて最初の prune で
        全部捨てられ、再起動のたびに 24h 窓を失う。
        """
        with self.lock:
            for addr, minutes in loaded.items():
                cur = self.buckets.setdefault(addr, {})
                m = self.meta.setdefault(addr, {})
                for minute, row in minutes.items():
                    if minute not in cur:
                        cur[minute] = row
                        # 市場補正の分次合計も戻す。0 から始めると再起動直後は
                        # 「市場は静か」扱いになって全銘柄が甘く採点される
                        self.market[minute] = self.market.get(minute, 0) + _count(row[2])
                    m["first_ts"] = min(m.get("first_ts", minute * 60), minute * 60)
                    m["last_ts"] = max(m.get("last_ts", 0), minute * 60 + 59)

    # --- 掃除 -------------------------------------------------------------

    def prune(self, now, bucket_hours=25):
        """窓から出た取引と古いバケットを落とす。長く動いていない銘柄ごと捨てる。"""
        cut = now - self.window
        bcut = int((now - bucket_hours * 3600) // 60)
        live_minutes = int((now - 600) // 60)
        with self.lock:
            for addr in list(self.trades):
                tr = self.trades[addr]
                i = 0
                while i < len(tr) and tr[i][0] < cut:
                    i += 1
                if i:
                    self.trades[addr] = tr[i:]
                if not self.trades[addr]:
                    del self.trades[addr]
            for addr in list(self.buckets):
                bk = self.buckets[addr]
                for minute in [m for m in bk if m < bcut]:
                    del bk[minute]
                # 生の窓から出たバケットの買い手は件数だけ残す（集合はメモリを食う）
                for minute, row in bk.items():
                    if minute < live_minutes and isinstance(row[2], set):
                        row[2] = len(row[2])
                if not bk:
                    del self.buckets[addr]
            # 名簿の命中・加速度のリング・市場の分次合計も同じ窓で切る
            for store, cut_ts in ((self.hits, now - LISTED_WINDOW),
                                  (self.g30, now - G30_RING)):
                for addr in list(store):
                    kept = [x for x in store[addr] if x[0] >= cut_ts]
                    if kept:
                        store[addr] = kept
                    else:
                        del store[addr]
            for minute in [m for m in self.market if m < bcut]:
                del self.market[minute]
            # 長く止まっている銘柄はバケットごと捨てる（常駐なのでメモリを無限に伸ばさない）。
            # 捨てても SQLite には残っているし、24h 系は API レーンの値を借りている。
            for addr in list(self.meta):
                if now - self.meta[addr].get("last_ts", 0) > IDLE_DROP:
                    del self.meta[addr]
                    self.buckets.pop(addr, None)
                    self.trades.pop(addr, None)
                    self.hits.pop(addr, None)
                    self.g30.pop(addr, None)

    def flush_rows(self, now=None, recent_min=15):
        """永続化したいバケット行を取り出す（呼んだら dirty は消える）。

        直近 recent_min 分だけを書く。閉じたバケットはもう変わらないので、毎回 1 日ぶん
        全部を書き直すと 1 銘柄 1,440 行 × 数百銘柄になって無駄に重い。
        """
        floor = int((now if now is not None else time.time()) // 60) - recent_min
        with self.lock:
            addrs, self.dirty = self.dirty, set()
            rows = []
            for addr in addrs:
                for minute, r in (self.buckets.get(addr) or {}).items():
                    if minute >= floor:
                        rows.append((addr, minute, r[0], r[1], _count(r[2]), r[3], r[4], r[5]))
        return rows

    # --- 参照 -------------------------------------------------------------

    def active_since(self, ts):
        with self.lock:
            return [a for a, m in self.meta.items() if m.get("last_ts", 0) >= ts]

    def size(self):
        with self.lock:
            return len(self.meta), sum(len(v) for v in self.trades.values())

    def _price_at(self, addr, now, minutes_ago, tolerance_min):
        """minutes_ago 分前（tolerance_min まで古くてよい）のバケットの終値。"""
        bk = self.buckets.get(addr) or {}
        target = int(now // 60) - minutes_ago
        best = None
        for minute, row in bk.items():
            if row[4] is None or minute > target or minute < target - tolerance_min:
                continue
            if best is None or minute > best[0]:
                best = (minute, row[4])
        return best[1] if best else None

    def row(self, addr, now):
        """rules.ROW_KEYS の正規化行（チェーンだけで出せる項目のみ埋める）。"""
        with self.lock:
            m = self.meta.get(addr)
            if not m:
                return None
            tr = [t for t in self.trades.get(addr, []) if t[0] >= now - self.window]
            buys = sum(1 for t in tr if t[1] == "buy")
            sells = len(tr) - buys
            buyers = len({t[3] for t in tr if t[1] == "buy"})
            vol5m = sum(t[2] for t in tr)
            price = m.get("price")
            first_price = next((t[4] for t in tr if t[4] > 0), None)
            pc5m = ((price / first_price - 1) * 100
                    if price and first_price and len(tr) >= 2 else None)
            p1h = self._price_at(addr, now, 60, 15)
            pc1h = (price / p1h - 1) * 100 if price and p1h else None
            p24h = self._price_at(addr, now, 1440, 240)
            pc24h = (price / p24h - 1) * 100 if price and p24h else None
            mcut = int(now // 60) - 1440
            vol24h = sum(r[3] for mi, r in (self.buckets.get(addr) or {}).items() if mi >= mcut)
            supply = m.get("supply")
            return {
                "chain": self.chain, "address": addr, "source": "chain",
                "symbol": m.get("symbol"), "name": None,
                "price": price, "mcap": price * supply if (price and supply) else None,
                "mcap_is_fdv": True, "liquidity": m.get("liq"),
                "buys5m": buys, "sells5m": sells, "buyers5m": buyers, "vol5m": vol5m,
                "vol24h": vol24h or None, "pc5m": pc5m, "pc1h": pc1h, "pc24h": pc24h,
                "holders": None, "smart": None, "renowned": None, "visiting": None,
                "created_ts": m.get("created_ts"), "launchpad": m.get("launchpad"),
                "wash": None, "honeypot": None, "dex": m.get("dex"),
                "_coverage": now - m.get("first_ts", now),
                "_last_ts": m.get("last_ts"), "_last_block": m.get("last_block"),
            }


    # --- EMS の特徴量 -----------------------------------------------------

    def push_g30(self, addr, ts, g):
        """G_U30 を覚えておく。加速度は 30 秒前との差なので自分で持つしかない。"""
        with self.lock:
            ring = [x for x in self.g30.get(addr, ()) if x[0] >= ts - G30_RING]
            ring.append((ts, float(g)))
            self.g30[addr] = ring

    def _g30_prev(self, addr, now, back=30, tol=15):
        """now−back 秒に最も近い G_U30（±tol 秒）。無ければ None。"""
        target, best = now - back, None
        for ts, g in self.g30.get(addr, ()):
            if abs(ts - target) <= tol and (best is None
                                            or abs(ts - target) < abs(best[0] - target)):
                best = (ts, g)
        return best[1] if best else None

    def _market_z(self, now):
        """チェーン全体の分次買い手合計の robust z（1 分に 1 回だけ計算する）。

        BSC 全体が沸く時間帯に全銘柄が同時に鳴るのを防ぐための補正。
        """
        minute = int(now // 60) - 1      # 直前の 1 分（今の分はまだ埋まっていない）
        if self._zm[0] == minute:
            return self._zm[1]
        vals = [v for mi, v in self.market.items() if mi < minute]
        z = 0.0
        if len(vals) >= 5:
            med = statistics.median(vals)
            mad = max(MARKET_MAD_FLOOR, statistics.median([abs(v - med) for v in vals]))
            z = (float(self.market.get(minute, 0)) - med) / mad
        self._zm = (minute, z)
        return z

    def _windows(self, addr, now):
        """10/30/120/300 秒の窓統計。新しい順に 1 回走査して 300 秒で打ち切る。"""
        spans = WINDOWS + (300,)
        buyers = {w: set() for w in spans}
        b = {w: 0 for w in spans}
        vb = {w: 0.0 for w in spans}
        vs = {w: 0.0 for w in spans}
        sellers, by_trader, prices = set(), {}, []
        for ts, side, usd, trader, price in reversed(self.trades.get(addr, ())):
            age = now - ts
            if age > 300:
                break
            if price > 0 and age <= 120:
                prices.append(price)
            for w in spans:
                if age > w:
                    continue
                if side == "buy":
                    b[w] += 1
                    vb[w] += usd
                    buyers[w].add(trader)
                else:
                    vs[w] += usd
            if side == "buy":
                by_trader[trader] = by_trader.get(trader, 0.0) + usd
            else:
                sellers.add(trader)
        out = {}
        for w in WINDOWS:
            out[f"U{w}"], out[f"B{w}"] = len(buyers[w]), b[w]
            out[f"Vb{w}"], out[f"Vs{w}"] = vb[w], vs[w]
            out[f"Net{w}"] = vb[w] - vs[w]
        both = vb[120] + vs[120]
        out["bsi"] = (vb[120] - vs[120]) / both if both > 0 else 0.0
        out["U300"], out["B300"] = len(buyers[300]), b[300]
        out["Top1_300"] = max(by_trader.values()) / vb[300] if vb[300] > 0 else 0.0
        out["roundtrip_300"] = (len(buyers[300] & sellers) / len(buyers[300])
                                if buyers[300] else 0.0)
        # prices は新しい順。2 件以上ないと「変化」が無い（初回の 1 件は 0% ではなく不明）
        out["pc120"] = ((prices[0] / prices[-1] - 1) * 100
                        if len(prices) >= 2 and prices[-1] > 0 else None)
        return out

    def _normal(self, addr, now, m, base):
        """平常値。窓の直前（最長窓 120 秒ぶん）で切り、15 分と 24h の厳しい方を採る。

        生後 60 分未満で発射台の分かる銘柄は、自分の 15 分/24h ではなく同齢コホートの
        p50 を平常値にする（生まれたては自分の履歴がほぼ無く、比も z も定義できない）。
        """
        floor_u, floor_v = baseline.floors(base)
        cut = int((now - BASE_CUT) // 60)
        short_from, long_from = cut - BASE_SHORT_MIN, cut - BASE_LONG_MIN
        u_short = u_long = 0
        logs_short, logs_long = [], []
        for minute, row in (self.buckets.get(addr) or {}).items():
            if minute >= cut or minute < long_from:
                continue
            u, lv = _count(row[2]), math.log1p(row[3] or 0.0)
            u_long += u
            logs_long.append(lv)
            if minute >= short_from:
                u_short += u
                logs_short.append(lv)
        created = m.get("created_ts")
        coh = baseline.cohort(base, m.get("launchpad"),
                              int((now - created) // 60) if created else None)
        if coh:
            mu_rate = max(coh[0], floor_u)
            floor_med = math.log1p(max(baseline.HARD_FLOOR_VOL, coh[1]))
        else:
            mu_rate = max(u_short / BASE_SHORT_MIN, u_long / BASE_LONG_MIN, floor_u)
            floor_med = math.log1p(floor_v)
        med_raw = statistics.median(logs_long) if logs_long else 0.0
        med = max(med_raw, statistics.median(logs_short) if logs_short else 0.0, floor_med)
        mad = MAD_FLOOR
        if len(logs_long) >= 3:
            mad = max(MAD_FLOOR, statistics.median([abs(x - med_raw) for x in logs_long]))
        return {"mu_rate": mu_rate, "med": med, "mad": mad, "cohort": bool(coh)}

    def price_series(self, addr, now, window=1800):
        """直近 window 秒の価格の並び [{ts, price}]。生の取引（5 分窓）はそのまま、
        その前は分次バケットの最終価格（ts は分の頭）。

        タイムラインの「上昇開始」用。DB のスナップショットは判定を通った銘柄にしか無く、
        初回の判定でそのまま通知になると 1 本も無い（2026-09-19 の初回通知が「上昇開始 ?」になった）。
        """
        with self.lock:
            raw = [(ts, p) for ts, _s, _u, _t, p in self.trades.get(addr, ())
                   if ts >= now - window and p and p > 0]
            first_raw = int(raw[0][0] // 60) if raw else int(now // 60) + 1
            out = [{"ts": minute * 60, "price": row[4]}
                   for minute, row in sorted((self.buckets.get(addr) or {}).items())
                   if minute < first_raw and minute * 60 >= now - window and row[4]]
            out.extend({"ts": ts, "price": p} for ts, p in raw)
            return out

    def features(self, addr, now, base=None):
        """EMS の特徴量（§12.2）。銘柄を知らなければ None。

        base は baseline.compute() の結果（全体フロアと同齢コホート）。
        """
        with self.lock:
            m = self.meta.get(addr)
            if not m:
                return None
            f = self._windows(addr, now)
            f.update(self._normal(addr, now, m, base))
            created = m.get("created_ts")
            raised = None
            # 作成から離れて観測を始めた銘柄（再起動後など）は端から数えていないので
            # 累計調達額を出さない（カーブ進捗を過小評価したまま採点してしまう）
            if (m.get("launchpad") == "four.meme" and created is not None
                    and m.get("raised_ts") is not None
                    and m["raised_ts"] - created <= FOURMEME_BIRTH):
                raised = m.get("raised")
            f.update({
                "wins": list(WINDOWS), "flow_w": 30,
                "address": addr, "symbol": m.get("symbol"),
                "first_ts": m.get("first_ts"),   # 最初に取引を見た時刻（タイムラインの初観測）
                "launchpad": m.get("launchpad"),
                "age_min": (now - created) / 60.0 if created else None,
                "price": m.get("price"), "liq": m.get("liq"),
                # 数えるのは「人」。同じ人の 2 ウォレットが両方買っても 1 人
                "n_listed_10m": len({(self.listed.get(w) or {}).get("name") or w
                                     for ts, w, side, _u, _p, _b in self.hits.get(addr, ())
                                     if side == "buy" and ts >= now - LISTED_WINDOW}),
                "raised_usd": raised,
                "bundle": bool(m.get("bundle_ts")
                               and now - m["bundle_ts"] <= BUNDLE_HOLD),
                "g30_prev": self._g30_prev(addr, now),
                "z_market": self._market_z(now),
                "last_ts": m.get("last_ts"), "last_block": m.get("last_block"),
            })
            return f


def now_minute(ts=None):
    return int((ts if ts is not None else time.time()) // 60)
