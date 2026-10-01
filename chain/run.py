"""BSC 高速レーンの常駐本体。

スレッドは 4 本:
  poll      0.4 秒ごとに eth_blockNumber を見て、新しいブロックのログを 1 コールで取り
            デコード → 集計。ここだけは絶対に止めない（DB にも Telegram にも触らない）。
  resolve   未知のペア/トークンのメタを eth_call のバッチで解決し、溜めてあったログを
            デコードし直して集計へ戻す。
  judge     2 秒ごとに「直近に取引があった銘柄」だけを rules.evaluate にかけ、
            API レーンと同じ events / クールダウン / ラダー段で発火判定して通知する。
  prefetch  銘柄を見つけた時点で通知に付ける画像（four.meme）と安全確認（GoPlus）を
            裏で埋める。送信経路からは取りに行かせない（撃つときに待たないため）。

判定と通知は BEGIN IMMEDIATE で events を読んでから書くので、API レーン（watch.py cycle）
と同時に走っても同じ段を二重に撃たない。
"""
import json
import os
import queue
import sys
import threading
import time

from . import agg as agg_mod
from . import decode, rpc as rpc_mod, store

import baseline   # noqa: E402  （chain/__init__.py が親をパスに入れている）
import db        # noqa: E402
import enrich    # noqa: E402
import notify    # noqa: E402
import rules     # noqa: E402
import score_log  # noqa: E402
import watch     # noqa: E402
from sources import dexscreener  # noqa: E402

# config.json に "chain" ブロックが無くても動くよう、既定はここに持つ（既存 config は編集しない）
DEFAULTS = {
    "poll_seconds": 0.4,
    "judge_seconds": 2.0,
    "max_catchup_blocks": 20,
    "resolve_batch": 16,
    "resolve_seconds": 0.35,
    "api_max_age_seconds": 600,   # tokens/snapshots の値をこの新しさまで借りる
    "rate_refresh_seconds": 600,
    "log_seconds": 10,
    "persist_seconds": 30,
    "config_reload_seconds": 60,
    "bucket_retention_days": 2,
}


def settings(cfg):
    out = dict(DEFAULTS)
    out.update(cfg.get("chain") or {})
    return out


class Stats:
    def __init__(self):
        self.blocks = 0
        self.swaps = 0
        self.fourmeme = 0
        self.flap = 0
        self.fired = 0
        self.sent = 0
        self.errors = 0       # 失敗した呼び出しの回数（同じブロックの再試行も数える）
        self.dropped = 0      # 取れずにやり直したブロックの数（重複なし）
        self.err_block = None
        self.lag = 0.0
        self.block = 0
        self.judged = 0       # EMS を採点した回数（シャドー運用の集計用）
        self.score_max = 0.0
        self.score_notified = {lv: 0 for lv in rules.LEVELS}


# --- メタ解決 --------------------------------------------------------------

class Resolver(threading.Thread):
    """未知のペア/トークンを eth_call のバッチで解決する。

    publicnode は JSON-RPC のバッチを受けるので、1 リクエストで 30〜50 コールまとめられる。
    初回起動は数百ペアが未知なので、ここが数分かけて追いつく（2 回目以降は DB のキャッシュ）。
    """

    def __init__(self, rpc, decoder, chain, sink, conf, stop, prefetch=None):
        super().__init__(daemon=True, name="resolve")
        self.rpc = rpc
        self.decoder = decoder
        self.chain = chain
        self.sink = sink
        self.conf = conf
        self.stop = stop
        self.prefetch = prefetch
        self.dec_cache = {}
        self.tries = {}
        self.max_tries = 3
        self.done = 0

    def run(self):
        conn = db.connect()
        store.ensure(conn)
        # 自動コミット。既定のままだと最初の save_* で始まった暗黙トランザクションが
        # 例外や次の RPC 待ちのあいだも開いたままになり、書き込みロックを握り続ける
        conn.isolation_level = None
        try:
            while not self.stop.is_set():
                needs = self.decoder.take_need(self.conf["resolve_batch"])
                if not needs:
                    self.stop.wait(self.conf["resolve_seconds"])
                    continue
                try:
                    self._resolve(conn, needs)
                    conn.commit()
                except Exception as e:  # ノードが変な値を返してもスレッドは落とさない
                    print(f"  [resolve] {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        finally:
            conn.close()

    # --- 下請け -----------------------------------------------------------

    def _call(self, to, data):
        return ("eth_call", [{"to": to, "data": data}, "latest"])

    def _prefetch_meta(self, address, meta):
        """four.meme の銘柄は見つけた時点で画像を先回りさせる（撃つ前に揃える）。

        生まれたてほど発火までが短いので、フィルタ通過ぶんより先に処理させる（hot）。
        """
        if self.prefetch and (meta or {}).get("launchpad") == "four.meme":
            self.prefetch.enqueue(address, meta, hot=True)

    def _decimals(self, tokens):
        """答えが返ったぶんだけ返す。返らなかったトークンは既定値で埋めない。

        桁を取り違えると価格が 10^9 倍ずれたまま永続化されるので、18 と決め打ちしない
        （取れなかったペアはそのまま置いて、次の取引でまた解決待ちに積まれる）。
        """
        want = [t for t in dict.fromkeys(tokens) if t not in self.dec_cache]
        if want:
            res = self.rpc.batch([self._call(t, decode.SEL_DECIMALS) for t in want])
            for t, r in zip(want, res):
                if r and len(r) >= 66:
                    self.dec_cache[t] = decode.u(r[2:])
        return {t: self.dec_cache[t] for t in tokens if t in self.dec_cache}

    def _resolve(self, conn, needs):
        """1 回のバッチで token0/token1（と four.meme の _tokenInfos）を引き、
        続けて decimals、さらに base トークンの totalSupply/symbol をまとめて引く。"""
        calls, plan = [], []
        for kind, address in needs:
            if kind == "pair":
                calls += [self._call(address, decode.SEL_TOKEN0),
                          self._call(address, decode.SEL_TOKEN1)]
                plan.append((kind, address, 2))
            elif kind == "fm":
                calls += [self._call(decode.FOURMEME, decode.SEL_TOKENINFOS
                                     + address[2:].rjust(64, "0")),
                          self._call(address, decode.SEL_SYMBOL)]
                plan.append((kind, address, 2))
            else:                     # base: ペアだけ既知で銘柄のメタが無いとき
                calls += [self._call(address, decode.SEL_DECIMALS),
                          self._call(address, decode.SEL_SUPPLY),
                          self._call(address, decode.SEL_SYMBOL)]
                plan.append((kind, address, 3))
        res = self.rpc.batch(calls)
        pairs, sides, i = [], [], 0
        for kind, address, n in plan:
            chunk = res[i:i + n]
            i += n
            if kind == "fm":
                meta = self._fm_meta(address, chunk)
                if meta is None:      # ノードが返さなかった。次の取引でまた積まれる
                    continue
                store.save_token(conn, self.chain, address, meta)
                self._prefetch_meta(address, meta)
                self.done += 1
                self.sink(self.decoder.apply_meta("fm", address, meta))
                continue
            if kind == "base":
                self._save_base(conn, address, self._base_meta(address, chunk))
                continue
            t0, t1 = chunk
            if not (t0 and t1 and len(t0) >= 66 and len(t1) >= 66):
                continue              # ノードが返さなかった。次の取引でまた積まれる
            t0, t1 = decode.addr(t0[2:]), decode.addr(t1[2:])
            picked = decode.pick_quote(t0, t1)
            if not picked:            # quote が無い＝USD 換算できないので永久に対象外
                store.save_pair(conn, self.chain, address, None)
                self.done += 1
                self.sink(self.decoder.apply_meta("pair", address, None))
                continue
            pairs.append((address, t0, t1, picked))
            sides += [t0, t1]
        if not pairs:
            return
        # 上の save_* で始まった書き込みを閉じてから次の RPC を待つ（ロックを握ったまま
        # ノードを待たない。Prefetch.fill と同じ理由）
        conn.commit()
        dec = self._decimals(sides)
        self._base_tokens(conn, [p[3][1] for p in pairs])
        for address, t0, t1, (quote, base) in pairs:
            if t0 not in dec or t1 not in dec:
                continue              # 桁が取れなかった。次の取引でまた積まれる
            meta = {"t0": t0, "t1": t1, "d0": dec[t0], "d1": dec[t1],
                    "quote": quote, "base": base,
                    "dex": self.decoder.pair_dex.get(address, "pancake")}
            store.save_pair(conn, self.chain, address, meta)
            self.done += 1
            self.sink(self.decoder.apply_meta("pair", address, meta))

    def _base_tokens(self, conn, bases):
        """FDV に要る総供給とシンボルを base トークンぶんまとめて引く。"""
        want = [b for b in dict.fromkeys(bases)
                if b not in decode.QUOTES and b not in self.decoder.tokens]
        if not want:
            return
        res = self.rpc.batch([c for b in want
                              for c in (self._call(b, decode.SEL_SUPPLY),
                                        self._call(b, decode.SEL_SYMBOL))])
        for k, base in enumerate(want):
            if base not in self.dec_cache:
                continue
            self._save_base(conn, base, self._token_meta(
                base, self.dec_cache[base], res[2 * k], res[2 * k + 1]))

    def _base_meta(self, address, chunk):
        """decimals / totalSupply / symbol の 3 本立て（桁が取れなければ諦めて再試行）。"""
        dec_, sup, sym = chunk
        if not (dec_ and len(dec_) >= 66):
            return None
        return self._token_meta(address, decode.u(dec_[2:]), sup, sym)

    def _token_meta(self, address, decimals, sup, sym):
        """総供給が取れなければ None（未解決扱い）。

        1 回のネットワーク失敗で「供給不明」を永続化すると、その銘柄は二度と FDV が
        出ない（実測でそうなった）。取れるまで数回やり直す。
        """
        if not (sup and len(sup) >= 66):
            return None
        hint = self.decoder.hint(address)
        return {"decimals": decimals, "supply": decode.u(sup[2:]) / 10 ** decimals,
                "symbol": _symbol(sym) or hint.get("symbol"), "quote": None,
                "launchpad": hint.get("launchpad"), "created_ts": hint.get("created_ts")}

    def _save_base(self, conn, address, meta):
        """解決できたら保存。何度やっても駄目な銘柄は空行で諦める（無限に撃たない）。"""
        if meta is None:
            self.tries[address] = self.tries.get(address, 0) + 1
            if self.tries[address] < self.max_tries:
                return
            hint = self.decoder.hint(address)
            meta = {"decimals": self.dec_cache.get(address, 18), "supply": None,
                    "symbol": hint.get("symbol"), "quote": None,
                    "launchpad": hint.get("launchpad"), "created_ts": hint.get("created_ts")}
        with self.decoder.lock:
            self.decoder.tokens[address] = meta
        store.save_token(conn, self.chain, address, meta)
        self._prefetch_meta(address, meta)
        self.done += 1

    def _fm_meta(self, address, chunk):
        """four.meme の _tokenInfos: [0]base [1]quote [3]totalSupply [9]lastPrice。

        quote が USDT のカーブと WBNB のカーブがあるので、ここで見ないと USD 換算を誤る。
        """
        info, sym = chunk
        if not info or len(info) < 2 + 64 * 10:
            return None
        w = decode.words(info)
        hint = self.decoder.hint(address)
        return {"decimals": 18, "supply": (decode.u(w[3]) / 1e18) or hint.get("supply"),
                "symbol": _symbol(sym) or hint.get("symbol"), "quote": decode.addr(w[1]),
                "launchpad": "four.meme", "created_ts": hint.get("created_ts")}


def _symbol(res):
    """symbol() の戻り。動的 string と bytes32 の両方がある。"""
    if not res or len(res) < 66:
        return None
    w = decode.words(res)
    if len(w) >= 3 and decode.u(w[0]) == 32:
        return decode.abi_string(w, 32).strip() or None
    try:
        return bytes.fromhex(w[0]).decode("utf-8", "ignore").rstrip("\x00").strip() or None
    except ValueError:
        return None


# --- 先回り取得 -------------------------------------------------------------

class Prefetch(threading.Thread):
    """通知に付ける画像（four.meme）と安全確認（GoPlus）を、撃つ前に裏で埋める。

    送信経路から取りに行くと 1 通ごとに外部 API の往復ぶん待たされる（「送信時に画像を
    取りに行く 2 秒も勿体無い」＝Tomato 指示）。銘柄を初めて見た時点でこちらが埋め、
    watch.send_alert はキャッシュだけを見る。無ければ画像なしで即テキストになる。

    列は 2 本で、生まれたての four.meme（hot）を先に処理する。発火まで数分しかない
    銘柄が、フィルタを通っただけの数百件の後ろで待つと画像が間に合わないため。hot は
    画像だけ引く（生まれたての銘柄は GoPlus がまだ知らず、聞いても空が返る）。

    外部 API は 1 件処理するごとに GAP 秒あけて 1 req/s 以下に抑える。同じ銘柄は
    列ごとに一度だけ積む（画像が無かったことも enrich 側が永続キャッシュする）。
    """

    GAP = 1.0        # 1 件処理したら空ける秒数（four.meme / GoPlus とも 1 req/s 以下に）

    def __init__(self, chain, stop):
        super().__init__(daemon=True, name="prefetch")
        self.chain = chain
        self.stop = stop
        self.hot = queue.Queue()      # 生まれたての four.meme（画像だけ・先に処理）
        self.queue = queue.Queue()    # フィルタを通った銘柄（画像＋安全確認）
        self.lock = threading.Lock()
        self.seen = set()
        self.seen_hot = set()
        self.queued = 0       # enqueue した累計
        self.done = 0         # 埋め終えた件数
        self.new_done = 0     # うち生まれたての four.meme
        self.logo_hits = 0    # 画像が用意できた件数
        self.errors = 0

    def enqueue(self, address, meta=None, hot=False, force=False):
        """まだ積んでいない銘柄なら積む。積んだら True。

        force=True は一度処理した銘柄でも積み直す（画像が後から用意される銘柄の再取得）。
        """
        if not address:
            return False
        seen, q = (self.seen_hot, self.hot) if hot else (self.seen, self.queue)
        with self.lock:
            if address in seen and not force:
                return False
            seen.add(address)
            self.queued += 1
        q.put((address, meta, hot))
        return True

    def take(self):
        """次に埋める 1 件 (address, meta, hot)。生まれたての four.meme を先に取る。"""
        try:
            return self.hot.get_nowait()
        except queue.Empty:
            pass
        try:
            return self.queue.get(timeout=0.5)
        except queue.Empty:
            return None

    def run(self):
        conn = db.connect()
        # 自動コミット。2026-09-19 02:36 に ensure_token の INSERT が UNIQUE 違反で失敗し、
        # 暗黙トランザクションが開いたまま列が空になって 18 分間ロックを握り続けた
        # （判定・API レーン・bot が全滅）。文ごとに確定すれば例外で握り残すことがない
        conn.isolation_level = None
        try:
            while not self.stop.is_set():
                item = self.take()
                if item is None:
                    continue
                address, meta, hot = item
                if self.fill(conn, address, meta=meta, hot=hot):
                    self.stop.wait(self.GAP)
        finally:
            conn.close()

    def fill(self, conn, address, now=None, meta=None, hot=False):
        """1 銘柄ぶん埋める。外部 API を叩きうる処理をしたら True（=次まで間を空ける）。"""
        now = int(now or time.time())
        meta = meta or {}
        try:
            # 生まれたての銘柄には tokens 行がまだ無い（judge_token が作る）。行が無いと
            # save_logo の UPDATE が空振りして画像が残らないので、判定の基準を持たない
            # 名札だけの行をここで作る。
            db.ensure_token(conn, self.chain, address, symbol=meta.get("symbol"),
                            launchpad=meta.get("launchpad"),
                            creation_ts=meta.get("created_ts"))
            # ここで一度確定する。INSERT で始まった暗黙トランザクションのまま four.meme /
            # GoPlus を待つと、その数秒間ずっと書き込みロックを握り続ける。列が空にならない
            # 間はそれが切れ目なく続き、判定スレッド・API レーン・bot が 30 秒待っても
            # 取れず落ちた（2026-09-18 16:00〜 の database is locked）。
            conn.commit()
            if enrich.logo(conn, self.chain, address, fetch=True, now=now):
                self.logo_hits += 1
            if not hot:
                enrich.security(conn, self.chain, address, now)
            conn.commit()     # enrich 側の _commit は暗黙トランザクション中は何もしない
        except Exception as e:      # 1 銘柄の失敗でスレッドを落とさない
            self.errors += 1
            print(f"  [prefetch] {address} {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
        self.done += 1
        self.new_done += 1 if hot else 0
        return True


# --- ブロックの取り込み -----------------------------------------------------

class Feed:
    def __init__(self, rpc, decoder, aggregate, conf, stats):
        self.rpc = rpc
        self.decoder = decoder
        self.agg = aggregate
        self.conf = conf
        self.stats = stats
        self.last_block = None

    def step(self):
        """新しいブロックがあれば取り込む。取りこぼしは max_catchup_blocks まで追う。"""
        bn, err = self.rpc.block_number()
        if bn is None:
            self.stats.errors += 1
            return 0
        if self.last_block is None:
            self.last_block = bn - 1
        if bn - self.last_block > self.conf["max_catchup_blocks"]:
            self.last_block = bn - self.conf["max_catchup_blocks"]
        done = 0
        for block in range(self.last_block + 1, bn + 1):
            if not self.block(block):
                # まだノードに伝わっていない等。次の周回でやり直す（取りこぼさない）。
                # 何度やっても駄目なら max_catchup_blocks の側で先へ飛ぶ。
                break
            self.last_block = block
            done += 1
        return done

    def block(self, block):
        """1 ブロック取り込む。取れたら True。"""
        logs, err = self.rpc.logs_by_topics(block, decode.TOPICS)
        if logs is None:
            logs = self._from_receipts(block)
        if logs is None:
            self.stats.errors += 1
            # step() は失敗したら break して次の周回で同じブロックをやり直す。
            # 直前と違うブロックのときだけ数えれば「落としたブロック数」になる。
            if block != self.stats.err_block:
                self.stats.dropped += 1
                self.stats.err_block = block
            return False
        ts = _block_ts(logs)
        trades = self.decoder.decode_logs(logs, ts, block)
        self.agg.add_many(trades)
        self.stats.blocks += 1
        self.stats.block = block
        self.stats.lag = time.time() - ts
        for t in trades:
            if t.dex == "four.meme":
                self.stats.fourmeme += 1
                continue
            self.stats.swaps += 1
            if t.launchpad == "flap":
                # flap の取引は V2 Swap として数えている。その内訳をログに出す
                self.stats.flap += 1
        return True

    def _from_receipts(self, block):
        """publicnode が落ちたとき用。レシート全部から対象のログだけ拾う。"""
        receipts, err = self.rpc.block_receipts(block)
        if not isinstance(receipts, list):
            return None
        want = set(decode.TOPICS)
        return [log for r in receipts for log in (r.get("logs") or [])
                if (log.get("topics") or [""])[0] in want]


def _block_ts(logs):
    for log in logs:
        raw = log.get("blockTimestamp")
        if raw:
            try:
                return int(raw, 16)
            except (TypeError, ValueError):
                pass
    return time.time()


# --- 判定と通知 -------------------------------------------------------------

def augment(conn, row, now, max_age):
    """チェーンだけでは出せない値を API レーンのスナップショットから借りる。

    保有者・スマートマネー・注目・24h は DexScreener / GMGN 側の値しかないので、
    max_age 秒以内に書かれたものだけを混ぜる（古い値で撃たないため）。
    """
    latest = db.latest(conn, row["chain"], row["address"])
    if not latest:
        return row
    snap_ts = rules.num(latest.get("ts"))
    fresh = snap_ts is not None and now - snap_ts <= max_age
    for key, src in (("symbol", "symbol"), ("launchpad", "launchpad"),
                     ("created_ts", "creation_ts"), ("name", "name")):
        if row.get(key) is None and latest.get(src) is not None:
            row[key] = latest[src]
    if not fresh:
        return row
    for key in ("holders", "smart", "renowned", "visiting", "wash", "honeypot"):
        if row.get(key) is None:
            row[key] = latest.get(key)
    if row.get("liquidity") is None:
        row["liquidity"] = latest.get("liquidity")
    # 自分の観測が 24h に満たない間は 24h 系を API の値で置き換える（過小評価を避ける）
    if row.get("_coverage", 0) < 23 * 3600:
        for key in ("vol24h", "pc24h"):
            if latest.get(key) is not None:
                row[key] = latest[key]
    if row.get("pc1h") is None:
        row["pc1h"] = latest.get("pc1h")
    return row


def load_wallets(cfg):
    """名簿ウォレット。1 行 `0xアドレス # 名前 | @ハンドル`（# 以降は任意）。無ければ空。

    戻りは {アドレス(小文字): {"name": 名前, "handle": ハンドル or None}}。
    名前が空ならアドレスの先頭 8 文字を名前にする（同じ名前＝同じ人の別ウォレット）。
    """
    name = (cfg.get("score") or {}).get("wallets_file") or "wallets.txt"
    path = name if os.path.isabs(name) else os.path.join(watch.HERE, name)
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                addr, _, comment = line.partition("#")
                addr = addr.strip().lower()
                if not addr:
                    continue
                label, _, handle = comment.partition("|")
                handle = handle.strip().lstrip("@").strip()
                out[addr] = {"name": label.strip() or addr[:8], "handle": handle or None}
    except OSError:
        pass
    return out


def listed_entries(conn, chain, address, now, aggregate, since_sec=86400):
    """その銘柄に入っている名簿の「人」の一覧（買い額の大きい順）。無ければ空。

    同じ名前の 2 ウォレットは 1 人に合算し、リンクは買い額の大きい方のウォレットに張る。
    """
    rows = db.wallet_entries(conn, chain, address, now - since_sec)
    if not rows:
        return []
    listed = aggregate.listed_info()
    people, tops = {}, {}
    for r in rows:
        info = listed.get(r["wallet"]) or {}
        name = info.get("name") or r["wallet"][:8]
        p = people.get(name)
        if p is None:
            p = people[name] = {"name": name, "handle": info.get("handle"),
                                "wallet": r["wallet"], "buy_usd": 0.0, "sell_usd": 0.0,
                                "buy_qty": 0.0, "first_buy_ts": None, "last_ts": 0,
                                "n_buys": 0, "n_sells": 0}
        if (r["buy_usd"] or 0.0) > tops.get(name, -1.0):
            tops[name], p["wallet"] = r["buy_usd"] or 0.0, r["wallet"]
        for key in ("buy_usd", "sell_usd", "buy_qty", "n_buys", "n_sells"):
            p[key] += r[key] or 0
        first = r["first_buy_ts"]
        if first is not None and (p["first_buy_ts"] is None or first < p["first_buy_ts"]):
            p["first_buy_ts"] = first
        p["last_ts"] = max(p["last_ts"], r["last_ts"] or 0)
    return sorted(people.values(), key=lambda p: p["buy_usd"], reverse=True)


def want_logo(conn, cfg, chain, address, score, now, prefetch=None):
    """点が付いてきた銘柄に画像が無ければ、先回りの列に積み直す。積んだら True。

    four.meme は銘柄が作られた直後だと画像をまだ登録していない（＝生まれた瞬間に
    先回りしても空振りする）。通知になりそうな点まで来たものだけ聞き直す。
    """
    if not prefetch:
        return False
    least = rules.num((cfg.get("score") or {}).get("db_min_score")) or 40
    if (rules.num(score) or 0) < least:
        return False
    tok = db.get_token(conn, chain, address)
    if enrich.local_logo(tok) or enrich.photo_url((tok or {}).get("logo")):
        return False
    if not enrich.can_fetch_logo(tok, now):
        return False
    return prefetch.enqueue(address, hot=True, force=True)


def score_token(conn, cfg, row, now, aggregate, base, alert=None, prefetch=None):
    """EMS（§12.3）で採点し、記録し、送ると決まれば通知する。採点できなければ None。

    旧ルールとは独立。ここで例外が出ても呼び出し側の発火・通知は止めない。
    """
    feat = aggregate.features(row["address"], now, base)
    if not feat:
        return None
    bnb = rules.num(getattr(aggregate, "bnb_usd", None))
    grad = (rules.num((cfg.get("score") or {}).get("fourmeme_grad_bnb")) or 18) * bnb \
        if bnb else None
    res = rules.score(feat, cfg, {
        "liquidity": row.get("liquidity"), "mcap": row.get("mcap"),
        "wash": row.get("wash"), "honeypot": row.get("honeypot"), "grad_usd": grad,
        "snapshots": row.get("_snapshots") or [], "holders": row.get("holders"),
        "smart": row.get("smart"), "visiting": row.get("visiting"), "now": now})
    # 名簿の人の建玉は本文にも JSONL にも出すので、送信判断より先に用意する
    res["entries"] = listed_entries(conn, row["chain"], row["address"], now, aggregate)
    # 加速度は 30 秒前の G との差なので、採点のたびに今の値を置いていく
    if res.get("g_accel") is not None:
        aggregate.push_g30(row["address"], now, res["g_accel"])
    # 送ると決まってからでは画像取得が間に合わないので、点が付いた時点で積み直す
    want_logo(conn, cfg, row["chain"], row["address"], res.get("score"), now, prefetch)
    send = None
    if alert:
        def send():
            # 本文は送ると決まってから組む。安全確認はキャッシュだけ見る（GoPlus を待たない）
            sec = enrich.cached_security(conn, row["chain"], row["address"])
            return alert(row, watch.build_score_message(row, res, feat, now, sec))
    res["decision"] = score_log.judge(
        conn, cfg, row["chain"], feat, res, now, send,
        {"liq": rules.num(row.get("liquidity")), "mcap": rules.num(row.get("mcap")),
         "lag_s": round(time.time() - (row.get("_last_ts") or now), 1),
         # タイムライン用（snapshots は JSONL には書かない＝SKIP_KEYS）
         "lane": "chain", "created_ts": watch._int(row.get("created_ts")),
         # DB のスナップショットは初回判定だと空なので、集計の価格列（生 5 分＋分次）も足す
         "snapshots": (row.get("_snapshots") or []) + aggregate.price_series(row["address"], now),
         "first_seen_ts": feat.get("first_ts"),
         "data_ts": rules.num(row.get("_last_ts")),
         "price": rules.num(row.get("price"))})
    return res


def judge_token(conn, cfg, row, now, aggregate=None, base=None, alert=None, prefetch=None):
    """1 銘柄を判定して events に書く。(本文, 発火数, 本文を持つ行の id) を返す。

    events の読み書きは BEGIN IMMEDIATE で囲む。API レーンも同じ表を使うので、
    読んだ後に相手が同じ段を書き込んで二重に撃つのを防ぐ。送信は commit の後にやる
    （ネットワーク待ちのあいだ書き込みロックを握らないため）。
    """
    chain, address = row["chain"], row["address"]
    conn.execute("BEGIN IMMEDIATE")
    try:
        token_row = db.upsert_token(conn, chain, address, {
            "symbol": row.get("symbol"), "name": row.get("name"),
            "launchpad": row.get("launchpad"),
            "creation_ts": watch._int(row.get("created_ts")),
            "price": rules.num(row.get("price")), "market_cap": rules.num(row.get("mcap")),
        }, now, True)
        history = db.get_history(conn, chain, address, now, watch.HISTORY_HOURS)
        last_ts = history["snapshots"][-1]["ts"] if history["snapshots"] else None
        gap = rules.num(cfg.get("snapshot_min_gap_seconds")) or 0
        if last_ts is None or now - last_ts >= gap:
            db.insert_snapshot(conn, chain, address, now, watch.snapshot_of(row))
        row["_snapshots"] = history["snapshots"]
        row["_recent"] = history["recent_triggers"]
        events = rules.evaluate(token_row, history, row, now, cfg)
        fired = sorted((e for e in events if e.notify), key=lambda e: e.trigger)
        message, carrier_id = "", None
        if events:
            immediate = bool(fired) and watch.is_immediate(
                row, fired, cfg, db.last_immediate_ts(conn, chain, address), now)
            # 本文は API レーンと同じ組み立て（源 chain の行で高速レーン発と分かる）。
            # GoPlus は 30 分キャッシュ・タイムアウト 2 秒で、即時に送る分だけ引く。
            message = watch.build_message(
                row, token_row, events, now, cfg,
                enrich.security(conn, chain, address, now)) if immediate else ""
            head = fired[0] if fired else None
            for e in events:
                carrier = e is head
                db.insert_event(conn, chain, address, now, e, message if carrier else "",
                                False, pending=1 if (e.notify and not immediate) else 0)
                if carrier:
                    carrier_id = conn.execute(
                        "SELECT last_insert_rowid() AS i").fetchone()["i"]
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    # EMS は commit の後（送信でネットワークを待つあいだロックを握らない）。
    # シャドー運用なので、ここが転んでも旧ルールの発火・通知は止めない。
    if aggregate is not None and (cfg.get("score") or {}).get("enabled", True):
        try:
            row["_score"] = score_token(conn, cfg, row, now, aggregate, base, alert,
                                        prefetch)
        except Exception as e:
            print(f"  [score] {row.get('symbol')} {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
    return message, len(fired), carrier_id


def judge_round(conn, cfg, aggregate, conf, alert, stats, now=None, prefetch=None,
                base=None):
    """直近に取引があった銘柄だけを判定する。発火した件数を返す。"""
    now = int(now or time.time())
    window = conf["judge_seconds"] * 2 + 1
    fired_total = 0
    for address in aggregate.active_since(now - window):
        row = aggregate.row(address, now)
        if not row or not row.get("price"):
            continue
        if not rules.passes_filters(row, cfg):   # API を引く前に安く落とす
            continue
        augment(conn, row, now, conf["api_max_age_seconds"])
        if not rules.passes_filters(row, cfg):
            continue
        if prefetch:
            # 撃つ前に画像と安全確認を裏で埋めさせる（同じ銘柄は初回だけ積まれる）
            prefetch.enqueue(row["address"])
        try:
            message, fired, carrier_id = judge_token(conn, cfg, row, now,
                                                     aggregate, base, alert, prefetch)
        except Exception as e:
            print(f"  [judge] {row.get('symbol')} {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            continue
        res = row.get("_score")
        if res:
            stats.judged += 1
            stats.score_max = max(stats.score_max, rules.num(res.get("score")) or 0.0)
            if res.get("decision") == "notified":
                lv = res.get("level")
                stats.score_notified[lv] = stats.score_notified.get(lv, 0) + 1
        if not fired:
            continue
        fired_total += fired
        stats.fired += fired
        # 「取引の入ったブロックの時刻」から「撃つと決めた時刻」までの実測（秒未満まで）
        lag = time.time() - (row.get("_last_ts") or now)
        print(f"[{watch.fmt_ts(now)}] fire {row.get('symbol') or '?'} {row['address']}"
              f" n={fired} block={row.get('_last_block')} lat={lag:.1f}s"
              f" imm={'y' if message else 'n'}", flush=True)
        if message and alert and cfg.get("notify", {}).get("legacy", True):
            ok = notify.sent_ok(alert(row, message))
            stats.sent += 1 if ok else 0
            if ok and carrier_id:
                conn.execute("UPDATE events SET sent=1, sent_ts=? WHERE id=?",
                             (now, carrier_id))
    return fired_total


class Judge(threading.Thread):
    def __init__(self, cfg, conf, aggregate, stats, stop, no_send=False, prefetch=None):
        super().__init__(daemon=True, name="judge")
        self.cfg = cfg
        self.conf = conf
        self.agg = aggregate
        self.stats = stats
        self.stop = stop
        self.no_send = no_send
        self.prefetch = prefetch
        self.base = None       # EMS の平常値（起動時と 6 時間ごとに作り直す）
        self._creds = None

    def alert(self, conn):
        """通知の送信関数（画像・安全確認・リンクボタン付き）。送らないときは None。

        dry_run は config.json を定期的に読み直して反映する。Telegram の送信 ON/OFF で
        設定が変わったとき、こちらだけ古い値で黙り続けるのを避けるため
        （しきい値も同じ理由で読み直す）。
        """
        if self.no_send or self.cfg.get("dry_run", True):
            return None
        if self._creds is None:
            self._creds = notify.credentials()
        token, chat_id = self._creds
        if not (token and chat_id):
            return None
        return lambda row, text: watch.send_alert(conn, self.cfg, row, text, (token, chat_id))

    def run(self):
        conn = db.connect()
        store.ensure(conn)
        conn.isolation_level = None      # BEGIN IMMEDIATE を自分で張るため
        next_persist = next_cfg = next_base = next_wallets = 0
        try:
            while not self.stop.is_set():
                t0 = time.time()
                try:
                    if t0 >= next_cfg:
                        self.cfg = watch.load_config()
                        self.conf = settings(self.cfg)
                        next_cfg = t0 + self.conf["config_reload_seconds"]
                    if t0 >= next_base:
                        # 中は 6 時間キャッシュ。作り直したときだけ 1 行出す
                        self.base, made = baseline.refresh(conn, self.agg.chain, t0)
                        if made:
                            print(f"  [score] {baseline.summary(self.base)}", flush=True)
                        next_base = t0 + 600
                    if t0 >= next_wallets:
                        self.agg.set_listed(load_wallets(self.cfg))
                        next_wallets = t0 + 300
                    # 名簿の生取引は判定の前に落としておく（この回の本文に出せるように）。
                    # autocommit なので executemany 1 回で終わり、ロックは握らない
                    db.insert_wallet_trades(conn, self.agg.chain,
                                            self.agg.take_listed_trades())
                    judge_round(conn, self.cfg, self.agg, self.conf, self.alert(conn),
                                self.stats, prefetch=self.prefetch, base=self.base)
                    if t0 >= next_persist:
                        self._persist(conn, t0)
                        next_persist = t0 + self.conf["persist_seconds"]
                except Exception as e:
                    self.stats.errors += 1
                    print(f"  [judge] {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                self.stop.wait(max(0.2, self.conf["judge_seconds"] - (time.time() - t0)))
        finally:
            conn.close()

    def _persist(self, conn, now):
        self.agg.prune(now)
        rows = self.agg.flush_rows(now)
        conn.execute("BEGIN IMMEDIATE")
        try:
            store.save_buckets(conn, self.agg.chain, rows)
            store.prune_buckets(conn, now, self.conf["bucket_retention_days"])
        finally:
            conn.execute("COMMIT")


# --- レート ----------------------------------------------------------------

def refresh_rates(rates, now=None):
    """WBNB / BNCB の USD を DexScreener で初期化・補正する（1 コール）。

    常時は自分が見た安定コイン建てペアの Sync で更新するが、起動直後は値が無いので
    ここで入れる。10 分ごとに引き直して、レート源のペアが止まっても腐らせない。
    """
    rows, status = dexscreener.fetch("bsc", [decode.WBNB, decode.BNCB])
    best = {}
    for r in rows:
        cur = best.get(r["address"])
        if cur is None or (r.get("liquidity") or 0) > (cur.get("liquidity") or 0):
            best[r["address"]] = r
    for address, r in best.items():
        rates.set(address, rules.num(r.get("price")), now or time.time())
    return status


# --- 常駐 ------------------------------------------------------------------

def build(cfg, chain="bsc"):
    """RPC・デコーダ・集計を組み立ててキャッシュを読み込む（テストからも使う）。"""
    conf = settings(cfg)
    rpc = rpc_mod.Rpc()
    rates = decode.Rates()
    decoder = decode.Decoder(rates)
    aggregate = agg_mod.Agg(chain)
    conn = db.connect()
    store.ensure(conn)
    try:
        decoder.pairs.update(store.load_pairs(conn, chain))
        decoder.tokens.update(store.load_tokens(conn, chain))
        aggregate.load_buckets(store.load_buckets(conn, chain, agg_mod.now_minute() - 1440))
    finally:
        conn.close()
    # レート源のペアは Swap を待たずに先に解決しておく（BNB 価格が無いと何も換算できない）
    for pair in decode.RATE_PAIRS:
        if pair not in decoder.pairs:
            decoder.need.append(("pair", pair))
    return conf, rpc, rates, decoder, aggregate


def heartbeat(conn, chain, stats, now):
    """高速レーンの生存を kv に 1 行置く（watch.py の /status がこれを読む）。

    API レーンは別プロセスなので、こちらが止まっていてもログを見ない限り分からなかった。
    """
    db.kv_set(conn, f"chain_{chain}_status", json.dumps(
        {"block": stats.block, "lag_s": round(stats.lag, 1), "ts": int(now)}))
    conn.commit()


def summary(stats, aggregate, decoder, rpc, elapsed, resolver, prefetch=None):
    tokens, trades = aggregate.size()
    per = max(elapsed, 0.001)
    pre = (f" prefetch={prefetch.done}/{prefetch.queued} new={prefetch.new_done}"
           f" logo={prefetch.logo_hits}" if prefetch else "")
    return (f"[{watch.fmt_ts(time.time())}] chain {aggregate.chain}"
            f" block={stats.block} lag={stats.lag:.1f}s"
            f" blocks/s={stats.blocks / per:.1f} swaps/s={stats.swaps / per:.1f}"
            f" 4meme/s={stats.fourmeme / per:.2f} flap/s={stats.flap / per:.2f}"
            f" tokens={tokens} trades={trades} fired={stats.fired} sent={stats.sent}"
            f" score: judged={stats.judged} max={stats.score_max:.0f} notified W/H/S/E="
            + "/".join(str(stats.score_notified.get(lv, 0)) for lv in rules.LEVELS) +
            f" resolve={resolver.done}/{decoder.pending_count()}{pre}"
            f" err={stats.errors}/{stats.dropped} rpc={rpc.stats_line()}")


def score_setup(cfg, aggregate, chain):
    """起動時に平常値と名簿を用意して 1 行出す（どちらも無くてもフロアだけで動く）。"""
    conn = db.connect()
    try:
        base, _ = baseline.refresh(conn, chain)
    finally:
        conn.close()
    wallets = load_wallets(cfg)
    aggregate.set_listed(wallets)
    print(f"[{watch.fmt_ts(time.time())}] score {baseline.summary(base)}"
          f" 名簿 {len(wallets)} 件", flush=True)
    return base


def main(chain="bsc", seconds=None, no_send=False):
    """常駐の本体。no_send=True なら config の dry_run に関係なく絶対に送らない。"""
    cfg = watch.load_config()
    conf, rpc, rates, decoder, aggregate = build(cfg, chain)
    stats = Stats()
    stop = threading.Event()
    refresh_rates(rates)
    aggregate.bnb_usd = rates.get(decode.WBNB)
    score_setup(cfg, aggregate, chain)
    feed = Feed(rpc, decoder, aggregate, conf, stats)
    prefetch = Prefetch(chain, stop)
    resolver = Resolver(rpc, decoder, chain, aggregate.add_many, conf, stop, prefetch)
    judge = Judge(cfg, conf, aggregate, stats, stop, no_send, prefetch)
    prefetch.start()
    resolver.start()
    judge.start()
    dry = no_send or cfg.get("dry_run", True)
    print(f"[{watch.fmt_ts(time.time())}] chain-watch start {chain} dry_run={dry}"
          f" pairs={len(decoder.pairs)} tokens={len(decoder.tokens)}"
          f" bnb=${rates.get(decode.WBNB) or 0:.2f}", flush=True)
    beat = db.connect()
    started = time.time()
    next_log = started + conf["log_seconds"]
    next_rate = started + conf["rate_refresh_seconds"]
    try:
        while not stop.is_set():
            t0 = time.time()
            try:
                feed.step()
            except Exception as e:      # ノード側の想定外。落とさず次のブロックへ
                stats.errors += 1
                print(f"  [feed] {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                stop.wait(5)
            now = time.time()
            if now >= next_log:
                print(summary(stats, aggregate, decoder, rpc, now - started, resolver,
                              prefetch), flush=True)
                try:
                    heartbeat(beat, chain, stats, now)
                except Exception as e:  # 生存記録のために取り込みを止めない
                    print(f"  [beat] {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                next_log = now + conf["log_seconds"]
            if now >= next_rate:
                refresh_rates(rates, now)
                aggregate.bnb_usd = rates.get(decode.WBNB)
                next_rate = now + conf["rate_refresh_seconds"]
            if seconds and now - started >= seconds:
                break
            stop.wait(max(0.0, conf["poll_seconds"] - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        judge.join(timeout=5)
        resolver.join(timeout=5)
        prefetch.join(timeout=5)
        beat.close()
    print(summary(stats, aggregate, decoder, rpc, max(time.time() - started, 1), resolver,
                  prefetch), flush=True)
    return stats, aggregate
