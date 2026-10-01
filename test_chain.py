"""オンチェーン高速レーンのテスト。

デコードの期待値は 2026-09-18 に BSC 本番から採ったログそのもの（下の FIX）。
ライブラリを跨いだ「引数の並びの思い込み」を固定するのが目的なので、合成データではなく
実ログを貼ってある。
"""
import json
import os
import sys
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db          # noqa: E402
import enrich      # noqa: E402
import rules       # noqa: E402
import watch       # noqa: E402
from chain import agg as agg_mod, decode, run as chain_run  # noqa: E402

# --- 実ログ（2026-09-18 BSC、ブロック 122521xxx 付近） ------------------------
# 公開用に、個人のウォレット（買い手・作成者・V3 の送り手）のアドレスだけダミーに置き換えてある。
# コントラクト・ペア・トークンのアドレスと金額は実ログのまま。

PAIR = "0xea4c038d26e3b6575d7d3ec1f0b47818bcc3b5ea"
TOKEN = "0x9701f3f9095dd4137995109cbaad9898d5cc7777"
BUYER = "0x00000000000000000000000000000000000000b1"

FIX = {
    # 同じ tx（0x1c8f498d…）に出た V2 Swap / Sync / flap TokenBought。
    # amount も cost も一致する＝flap のカーブは本物の V2 ペア。
    "v2": {
        "address": PAIR,
        "topics": [decode.V2_SWAP,
                   "0x000000000000000000000000e2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0",
                   "0x000000000000000000000000" + BUYER[2:]],
        "data": "0x" + "00" * 32
                + "000000000000000000000000000000000000000000000000001195ffafa36000"
                + "00000000000000000000000000000000000000000000b9106b5d500268356ca1"
                + "00" * 32,
    },
    "sync": {
        "address": PAIR,
        "topics": [decode.SYNC],
        "data": "0x00000000000000000000000000000000000000000392ff162857f71bcf0a935f"
                "0000000000000000000000000000000000000000000000005546e56d7cb43000",
    },
    "flap_buy": {
        "address": decode.FLAP,
        "topics": [decode.FLAP_BOUGHT],
        "data": "0x000000000000000000000000000000000000000000000000000000006aac9a00"
                "0000000000000000000000009701f3f9095dd4137995109cbaad9898d5cc7777"
                "00000000000000000000000000000000000000000000000000000000000000b1"
                "00000000000000000000000000000000000000000000b9106b5d500268356ca1"
                "000000000000000000000000000000000000000000000000001195ffafa36000"
                "00000000000000000000000000000000000000000000000000002d051dea9800"
                "000000000000000000000000000000000000000000000000000000014b1c2e72",
    },
    "v3": {
        "address": "0x46ee2c5f2b9de7c6e08ffe3bde8dd88a46d6f568",
        "topics": [decode.V3_SWAP,
                   "0x00000000000000000000000000000000000000000000000000000000000000b3",
                   "0x00000000000000000000000000000000000000000000000000000000000000b3"],
        "data": "0x0000000000000000000000000000000000000000000000177fba9c07edeb82af"
                "ffffffffffffffffffffffffffffffffffffffffffffffe884ac532c247f36d0"
                "0000000000000000000000000000000000000000ffe81221332618e0483de035"
                "000000000000000000000000000000000000000003dae028d0123bcd84ba6291"
                "fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff8",
    },
    "fm_buy": {
        "address": decode.FOURMEME,
        "topics": [decode.FM_PURCHASE],
        "data": "0x000000000000000000000000dfe00d2cbe719eabc924e4f712fbc5a2ccf44444"
                "00000000000000000000000000000000000000000000000000000000000000b2"
                "000000000000000000000000000000000000000000000000000000016637ef24"
                "000000000000000000000000000000000000000000002eaf7922ef8bf2b13e00"
                "0000000000000000000000000000000000000000000000000004b4cef93a0d55"
                "00000000000000000000000000000000000000000000000000000c0c4f4ad20d"
                "000000000000000000000000000000000000000002818d20526c246077580200"
                "00000000000000000000000000000000000000000000000001fd62f85c3e1860",
    },
    "fm_create": {
        "address": decode.FOURMEME,
        "topics": [decode.FM_CREATE],
        "data": "0x00000000000000000000000000000000000000000000000000000000000000c1"
                "0000000000000000000000004192d98c105251b82cb5c2e3700c2855d82cffff"
                "0000000000000000000000000000000000000000000000000000000006220ff5"
                "0000000000000000000000000000000000000000000000000000000000000100"
                "0000000000000000000000000000000000000000000000000000000000000140"
                "0000000000000000000000000000000000000000033b2e3c9fd0803ce8000000"
                "0000000000000000000000000000000000000000000000000000000000000000"
                "0000000000000000000000000000000000000000000000000000000000000000"
                "000000000000000000000000000000000000000000000000000000000000000a"
                "53616e7461204361736800000000000000000000000000000000000000000000"
                "0000000000000000000000000000000000000000000000000000000000000005"
                "2443415348000000000000000000000000000000000000000000000000000000",
    },
}

FM_TOKEN = "0xdfe00d2cbe719eabc924e4f712fbc5a2ccf44444"
BNB = 743.0


def decoder(rate=BNB, pair_meta=None):
    d = decode.Decoder()
    d.rates.set(decode.WBNB, rate)
    d.pairs[PAIR] = pair_meta or {
        "t0": TOKEN, "t1": decode.WBNB, "d0": 18, "d1": 18,
        "quote": decode.WBNB, "base": TOKEN, "dex": "pancake-v2"}
    return d


class TestDecodeSwap(unittest.TestCase):
    def test_v2_buy(self):
        """quote（WBNB）が入って base が出る＝買い。USD も価格も quote 側から出す。"""
        d = decoder()
        t = d.decode(FIX["v2"], 1000.0, 42)
        self.assertEqual(t.side, "buy")
        self.assertEqual(t.address, TOKEN)
        self.assertEqual(t.trader, BUYER)
        self.assertAlmostEqual(t.usd, 0.00495 * BNB, places=6)      # 0.00495 WBNB
        self.assertAlmostEqual(t.price, 5.663999963073475e-09 * BNB, places=12)
        self.assertEqual(t.dex, "pancake-v2")

    def test_v2_sell(self):
        """向きを入れ替えたら売りになる（base 入り・quote 出）。"""
        d = decoder()
        log = dict(FIX["v2"])
        w = decode.words(log["data"])
        log["data"] = "0x" + w[2] + w[3] + w[0] + w[1]   # a0in/a1in/a0out/a1out を入替
        t = d.decode(log, 1000.0, 42)
        self.assertEqual(t.side, "sell")
        self.assertAlmostEqual(t.usd, 0.00495 * BNB, places=6)

    def test_sync_sets_liquidity(self):
        """V2 の Sync は reserves。流動性は quote 側 × 2 × レート。"""
        d = decoder()
        d.decode(FIX["sync"], 1000.0, 42)
        t = d.decode(FIX["v2"], 1000.0, 42)
        self.assertAlmostEqual(t.liq, 6.144851 * BNB * 2, places=3)

    def test_quote_pair_is_not_a_token(self):
        """WBNB/USDT のような quote 同士のペアは銘柄として数えない。"""
        d = decoder(pair_meta={"t0": decode.USDT, "t1": decode.WBNB, "d0": 18, "d1": 18,
                               "quote": decode.USDT, "base": decode.WBNB, "dex": "v2"})
        self.assertIsNone(d.decode(FIX["v2"], 1000.0, 42))

    def test_unknown_pair_is_buffered_then_replayed(self):
        """未知のペアは捨てずに溜めて、メタが入った時点でデコードし直す。"""
        d = decode.Decoder()
        d.rates.set(decode.WBNB, BNB)
        self.assertIsNone(d.decode(FIX["v2"], 1000.0, 42))
        self.assertEqual(d.take_need(), [("pair", PAIR)])
        trades = d.apply_meta("pair", PAIR, {
            "t0": TOKEN, "t1": decode.WBNB, "d0": 18, "d1": 18,
            "quote": decode.WBNB, "base": TOKEN, "dex": "pancake-v2"})
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].side, "buy")

    def test_missing_supply_is_queued_without_dropping_the_trade(self):
        """ペアだけ既知で銘柄のメタが無いとき、取引は流しつつ総供給を取りに行く。

        古いキャッシュのペアは base の totalSupply を持っていないことがあり、
        そのままだと FDV が永久に None になる。
        """
        d = decoder()
        t = d.decode(FIX["v2"], 1000.0, 42)
        self.assertEqual(t.side, "buy")
        self.assertIsNone(t.supply)
        self.assertEqual(d.take_need(), [("base", TOKEN)])

    def test_v3_price_from_sqrt(self):
        """V3 は売買と USD を amount から、価格は sqrtPriceX96 から出す。"""
        pool = FIX["v3"]["address"]
        d = decode.Decoder()
        d.rates.set(decode.USDT, 1.0)
        d.pairs[pool] = {"t0": decode.USDT, "t1": "0xbase", "d0": 18, "d1": 18,
                         "quote": decode.USDT, "base": "0xbase", "dex": "pancake-v3"}
        t = d.decode(FIX["v3"], 1000.0, 42)
        self.assertEqual(t.side, "buy")             # quote が入り base が出た
        self.assertAlmostEqual(t.usd, 433.478954041675, places=6)
        # quote が token0 なので base の価格は sqrt 由来の token1/token0 の逆数
        self.assertAlmostEqual(t.price, 1.0 / 0.999269872672227, places=9)


class TestDecodeLaunchpad(unittest.TestCase):
    def test_fourmeme_buy(self):
        """cost == amount × price / 1e18 が four.meme の不変条件。"""
        e = decode.fourmeme_trade(FIX["fm_buy"])
        self.assertEqual(e["side"], "buy")
        self.assertEqual(e["token"], FM_TOKEN)
        self.assertAlmostEqual(e["amount"] * e["price"], e["cost"], places=6)
        d = decode.Decoder()
        d.rates.set(decode.USDT, 1.0)
        d.tokens[FM_TOKEN] = {"decimals": 18, "supply": 1e9, "symbol": "X",
                              "quote": decode.USDT, "launchpad": "four.meme",
                              "created_ts": 900}
        t = d.decode(FIX["fm_buy"], 1000.0, 42)
        self.assertEqual(t.dex, "four.meme")
        self.assertAlmostEqual(t.usd, 0.001324700944436565, places=9)
        self.assertAlmostEqual(t.price, 6.009909028e-09, places=15)
        self.assertEqual(t.supply, 1e9)

    def test_fourmeme_sale_side(self):
        log = dict(FIX["fm_buy"], topics=[decode.FM_SALE])
        self.assertEqual(decode.fourmeme_trade(log)["side"], "sell")

    def test_fourmeme_needs_quote_before_counting(self):
        """quote（USDT 建てか BNB 建てか）が分かるまでは取引にしない。"""
        d = decode.Decoder()
        d.rates.set(decode.USDT, 1.0)
        d.tokens[FM_TOKEN] = {"decimals": 18, "supply": 1e9, "symbol": None, "quote": None}
        self.assertIsNone(d.decode(FIX["fm_buy"], 1000.0, 42))
        self.assertEqual(d.take_need(), [("fm", FM_TOKEN)])

    def test_fourmeme_create(self):
        c = decode.fourmeme_create(FIX["fm_create"])
        self.assertEqual(c["token"], "0x4192d98c105251b82cb5c2e3700c2855d82cffff")
        self.assertEqual(c["name"], "Santa Cash")
        self.assertEqual(c["symbol"], "$CASH")
        self.assertEqual(c["supply"], 1e9)

    def test_flap_is_not_counted_twice(self):
        """flap のカーブは V2 ペアそのもの。TokenBought は取引にせず印だけ付ける。"""
        d = decoder()
        self.assertIsNone(d.decode(FIX["flap_buy"], 1000.0, 42))
        self.assertEqual(d.hints[TOKEN]["launchpad"], "flap")
        # 同じ tx の V2 Swap 側だけが取引になる
        t = d.decode(FIX["v2"], 1000.0, 42)
        self.assertEqual(t.launchpad, "flap")
        e = decode.flap_trade(FIX["flap_buy"])
        v = decode.v2_swap(FIX["v2"])
        self.assertAlmostEqual(e["cost"], v[1] / 1e18, places=12)   # 同額＝二重計上の証拠


class TestSymbol(unittest.TestCase):
    def test_dynamic_string(self):
        data = ("0x" + "20".rjust(64, "0")
                + "3".rjust(64, "0") + "4142430000000000000000000000000000000000000000"
                                       "00000000000000000000".ljust(64, "0")[:64])
        self.assertEqual(chain_run._symbol(data), "ABC")

    def test_bytes32(self):
        self.assertEqual(chain_run._symbol("0x" + "414243".ljust(64, "0")), "ABC")


class FakeTrade:
    def __init__(self, ts, side, usd, trader, price, address="0xtok", **kw):
        self.ts, self.side, self.usd, self.trader, self.price = ts, side, usd, trader, price
        self.address, self.block, self.dex = address, 1, "pancake-v2"
        self.liq = kw.get("liq")
        self.supply = kw.get("supply")
        self.symbol = kw.get("symbol")
        self.launchpad = kw.get("launchpad")
        self.created_ts = kw.get("created_ts")


class TestAgg(unittest.TestCase):
    def test_price_series_mixes_raw_trades_and_minute_buckets(self):
        a = agg_mod.Agg("bsc")
        now = 10_000_000
        a.add(FakeTrade(now - 1500, "buy", 10, "A", 1.0))    # 25 分前（掃除で生の窓を出る）
        a.add(FakeTrade(now - 1200, "buy", 10, "B", 2.0))
        a.add(FakeTrade(now - 3600, "buy", 10, "Z", 0.5))    # 窓の外
        a.prune(now)
        a.add(FakeTrade(now - 100, "buy", 10, "C", 3.0))
        a.add(FakeTrade(now - 10, "buy", 10, "D", 4.0))
        series = a.price_series("0xtok", now)
        self.assertEqual([s["price"] for s in series], [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(series[0]["ts"], (now - 1500) // 60 * 60)   # バケットは分の頭
        self.assertEqual(series[-1]["ts"], now - 10)                  # 生の取引はそのまま
        self.assertEqual(a.price_series("0xnope", now), [])

    def test_features_carry_the_first_trade_time(self):
        a = agg_mod.Agg("bsc")
        now = 10_000_000
        a.add(FakeTrade(now - 200, "buy", 10, "A", 1.0))
        a.add(FakeTrade(now - 10, "buy", 10, "B", 1.5))
        self.assertEqual(a.features("0xtok", now)["first_ts"], now - 200)

    def test_window_and_unique_buyers(self):
        a = agg_mod.Agg("bsc")
        now = 10_000_000
        a.add(FakeTrade(now - 400, "buy", 100, "A", 1.0, supply=1e6))   # 5 分窓の外
        for i in range(4):
            a.add(FakeTrade(now - 100 + i, "buy", 10, "A", 2.0))        # 同じ買い手 4 回
        a.add(FakeTrade(now - 50, "buy", 10, "B", 2.0))
        a.add(FakeTrade(now - 10, "sell", 5, "C", 2.0))
        row = a.row("0xtok", now)
        self.assertEqual(row["buys5m"], 5)
        self.assertEqual(row["sells5m"], 1)
        self.assertEqual(row["buyers5m"], 2)                           # 実数は 2 人
        self.assertAlmostEqual(row["vol5m"], 55)   # 5 分窓の外の 100 は数えない
        self.assertEqual(row["mcap"], 2.0 * 1e6)
        self.assertTrue(row["mcap_is_fdv"])

    def test_price_change(self):
        a = agg_mod.Agg("bsc")
        now = 10_000_000
        a.add(FakeTrade(now - 200, "buy", 10, "A", 1.0))
        a.add(FakeTrade(now - 10, "buy", 10, "B", 1.5))
        self.assertAlmostEqual(a.row("0xtok", now)["pc5m"], 50.0)

    def test_pc1h_from_buckets(self):
        a = agg_mod.Agg("bsc")
        now = 10_000_000
        a.add(FakeTrade(now - 3600, "buy", 10, "A", 1.0))   # 60 分前のバケット
        a.add(FakeTrade(now - 10, "buy", 10, "B", 3.0))
        row = a.row("0xtok", now)
        self.assertAlmostEqual(row["pc1h"], 200.0)
        self.assertAlmostEqual(row["vol24h"], 20.0)

    def test_prune_keeps_buckets(self):
        a = agg_mod.Agg("bsc")
        now = 10_000_000
        a.add(FakeTrade(now - 1000, "buy", 10, "A", 1.0))   # 生の窓の外
        a.add(FakeTrade(now - 400, "buy", 10, "B", 1.0))    # 生の窓の外・書き出しの窓の内
        a.prune(now)
        self.assertNotIn("0xtok", a.trades)          # 生の取引は落ちる
        self.assertEqual(len(a.buckets["0xtok"]), 2)  # バケットは残る
        old = a.buckets["0xtok"][(now - 1000) // 60]
        self.assertEqual(old[2], 1)                  # 10 分より古い買い手は件数に畳む
        rows = a.flush_rows(now)                     # 書き直すのは直近 15 分だけ
        self.assertEqual([(r[0], r[1]) for r in rows], [("0xtok", (now - 400) // 60)])
        self.assertEqual(rows[0][4], 1)              # 買い手は件数で永続化

    def test_prune_drops_idle_tokens(self):
        """止まった銘柄はバケットごと捨てる（常駐なのでメモリを伸ばし続けない）。"""
        a = agg_mod.Agg("bsc")
        now = 10_000_000
        a.add(FakeTrade(now - 3 * 3600, "buy", 10, "A", 1.0))
        a.prune(now)
        self.assertEqual(a.meta, {})
        self.assertEqual(a.buckets, {})

    def test_loaded_buckets_survive_the_first_prune(self):
        """再起動で読み戻したバケットは、直近のものなら残る。"""
        a = agg_mod.Agg("bsc")
        now = 10_000_000
        minute = now // 60 - 30
        a.load_buckets({"0xtok": {minute: [3, 1, 2, 500.0, 1.5, 1.7]},
                        "0xold": {now // 60 - 600: [1, 0, 1, 9.0, 0.5, 0.5]}})
        a.prune(now)
        self.assertIn("0xtok", a.buckets)
        self.assertNotIn("0xold", a.buckets)


class FakeRpc:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def batch(self, calls, timeout=None):
        self.calls.append(calls)
        return self.results.pop(0) if self.results else [None] * len(calls)


class TestResolverRetry(unittest.TestCase):
    """総供給が 1 回取れなかっただけで「供給不明」を永続化しない（FDV が永久に None になる）。"""

    def setUp(self):
        self.conn = db.connect(":memory:")
        from chain import store
        store.ensure(self.conn)
        self.dec = decode.Decoder()
        self.res = chain_run.Resolver(FakeRpc([]), self.dec, "bsc", lambda t: None,
                                      chain_run.DEFAULTS, None)

    def tearDown(self):
        self.conn.close()

    def _saved(self):
        return self.conn.execute(
            "SELECT total_supply FROM chain_tokens WHERE address='0xtok'").fetchall()

    def test_transient_failure_is_retried_then_given_up(self):
        for _ in range(chain_run.Resolver(None, None, "", None, {}, None).max_tries - 1):
            self.res._save_base(self.conn, "0xtok", None)
            self.assertEqual(self._saved(), [])          # まだ保存しない＝また取りに行く
        self.res._save_base(self.conn, "0xtok", None)     # 3 回目で諦めて空行
        self.assertEqual(len(self._saved()), 1)
        self.assertIsNone(self._saved()[0]["total_supply"])

    def test_success_saves_supply(self):
        meta = self.res._token_meta("0xtok", 18, "0x" + hex(10**27)[2:].rjust(64, "0"), None)
        self.assertEqual(meta["supply"], 1e9)
        self.res._save_base(self.conn, "0xtok", meta)
        self.assertEqual(self._saved()[0]["total_supply"], 1e9)

    def test_no_supply_answer_is_not_a_meta(self):
        self.assertIsNone(self.res._token_meta("0xtok", 18, None, None))


class FakeRpcBlocks:
    """block_number は常に same、getLogs は指定したブロックだけ失敗させる。"""

    def __init__(self, head, fail_on=()):
        self.head = head
        self.fail_on = set(fail_on)
        self.seen = []

    def block_number(self):
        return self.head, None

    def logs_by_topics(self, block, topics):
        self.seen.append(block)
        return (None, "err") if block in self.fail_on else ([], None)

    def block_receipts(self, block):
        return None, "err"


class TestFeed(unittest.TestCase):
    def test_failed_block_is_retried_not_skipped(self):
        """取れなかったブロックで止まり、次の周回でやり直す（取引を取りこぼさない）。"""
        rpc = FakeRpcBlocks(105, fail_on={103})
        feed = chain_run.Feed(rpc, decode.Decoder(), agg_mod.Agg("bsc"),
                              chain_run.DEFAULTS, chain_run.Stats())
        feed.last_block = 101
        self.assertEqual(feed.step(), 1)          # 102 まで進んで 103 で止まる
        self.assertEqual(feed.last_block, 102)
        rpc.fail_on.clear()
        self.assertEqual(feed.step(), 3)          # 103〜105 を取り直す
        self.assertEqual(feed.last_block, 105)

    def test_catchup_is_bounded(self):
        """遅れすぎたら追いかけずに先頭付近へ飛ぶ（永久に追いつけなくならない）。"""
        rpc = FakeRpcBlocks(1000)
        feed = chain_run.Feed(rpc, decode.Decoder(), agg_mod.Agg("bsc"),
                              chain_run.DEFAULTS, chain_run.Stats())
        feed.last_block = 1
        feed.step()
        self.assertEqual(feed.last_block, 1000)
        self.assertEqual(len(rpc.seen), chain_run.DEFAULTS["max_catchup_blocks"])


def _cfg():
    return json.loads(json.dumps(watch.DEFAULTS))


def _row(**kw):
    row = {k: None for k in rules.ROW_KEYS}
    row.update({"chain": "bsc", "address": "0xabc", "source": "chain", "symbol": "TEST",
                "price": 0.0011, "mcap": 1_100_000, "mcap_is_fdv": True,
                "liquidity": 120_000, "buys5m": 40, "sells5m": 10, "buyers5m": 25,
                "vol5m": 9000, "vol24h": 300_000, "pc5m": 5, "pc1h": 50, "pc24h": 60,
                "created_ts": 1_000_000 - 86400, "dex": "pancake-v2"})
    row.update(kw)
    return row


class TestJudge(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.conn.isolation_level = None
        self.now = 1_000_000
        # 本文の組み立てから GoPlus を引くので、テストではネットワークに出さない
        pat = mock.patch.object(chain_run.enrich, "security", return_value=None)
        pat.start()
        self.addCleanup(pat.stop)
        # 30 万ドルから上がってきた状態を作る（M1 の跨ぎ判定は自分の観測が要る）
        db.insert_snapshot(self.conn, "bsc", "0xabc", self.now - 600,
                           {"price": 0.0009, "mcap": 900_000, "vol5m": 8000,
                            "buys5m": 30, "sells5m": 12, "pc1h": 40})

    def tearDown(self):
        self.conn.close()

    def test_fires_once_per_stage(self):
        cfg = _cfg()
        msg, fired, _id = chain_run.judge_token(self.conn, cfg, _row(), self.now)
        self.assertEqual(fired, 1)
        rows = self.conn.execute(
            'SELECT "trigger",stage,notified FROM events ORDER BY stage').fetchall()
        hit = [r for r in rows if r["notified"]]
        self.assertEqual((hit[0]["trigger"], hit[0]["stage"]), ("M1", "1000000"))
        # 2 回目（API レーンでも高速レーンでも）同じ段は撃たない
        msg2, fired2, _ = chain_run.judge_token(self.conn, cfg, _row(), self.now + 1)
        self.assertEqual(fired2, 0)
        self.assertEqual(msg2, "")
        n = self.conn.execute(
            'SELECT COUNT(*) AS n FROM events WHERE "trigger"="M1" AND stage="1000000"'
            " AND notified=1").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_message_marks_the_chain_lane(self):
        msg, fired, carrier = chain_run.judge_token(self.conn, _cfg(), _row(), self.now)
        self.assertTrue(msg.startswith("<b>【やばい】$TEST</b>  BSC"))
        self.assertIn("M1: 時価総額 $1M 到達", msg)
        self.assertIn("・ 源 chain", msg)        # 高速レーン発だと本文から分かる
        self.assertIsNotNone(carrier)
        # 送信は commit の後にやるので、この時点では未送信のまま記録されている
        self.assertEqual(self.conn.execute(
            "SELECT sent FROM events WHERE id=?", (carrier,)).fetchone()["sent"], 0)

    def test_small_token_goes_to_the_batch(self):
        """やばくない・大きくもない銘柄は即時にせず、まとめ待ち（pending）に積む。"""
        cfg = _cfg()
        db.insert_snapshot(self.conn, "bsc", "0xdef", self.now - 600,
                           {"price": 0.0002, "mcap": 200_000, "vol5m": 8000})
        row = _row(address="0xdef", price=0.00026, mcap=260_000, vol24h=300_000)
        msg, fired, _ = chain_run.judge_token(self.conn, cfg, row, self.now)
        self.assertEqual(msg, "")
        self.assertEqual(fired, 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE address='0xdef' AND pending=1"
        ).fetchone()["n"], 1)

    def test_batch_minutes_zero_sends_even_without_api_fields(self):
        """RPC 側で条件を満たしたら、保有者・スマート・注目等（API 側でしか埋まらない
        フィールド）が無くても batch_minutes=0 なら即時に送る（まとめを待たない）。"""
        cfg = _cfg()
        cfg["notify"]["batch_minutes"] = 0
        db.insert_snapshot(self.conn, "bsc", "0xd11", self.now - 600,
                           {"price": 0.0002, "mcap": 200_000, "vol5m": 8000})
        # _row() の holders/smart/renowned/visiting/launchpad は既定で None のまま。
        # mcap $260K・vol24h $300K は notify.immediate の閾値未満＝通常なら即時にならない規模。
        row = _row(address="0xd11", price=0.00026, mcap=260_000, vol24h=300_000)
        msg, fired, carrier = chain_run.judge_token(self.conn, cfg, row, self.now)
        self.assertEqual(fired, 1)
        self.assertTrue(msg)
        self.assertNotIn("None", msg)
        self.assertIsNotNone(carrier)
        self.assertEqual(self.conn.execute(
            "SELECT pending FROM events WHERE id=?", (carrier,)).fetchone()["pending"], 0)

    def test_augment_borrows_only_fresh_api_values(self):
        db.upsert_token(self.conn, "bsc", "0xabc", {"symbol": "TEST", "price": 0.001,
                                                    "market_cap": 1_000_000}, self.now)
        db.insert_snapshot(self.conn, "bsc", "0xabc", self.now - 60,
                           {"holders": 4321, "smart": 7, "vol24h": 5_000_000})
        row = chain_run.augment(self.conn, _row(vol24h=None), self.now, 600)
        self.assertEqual(row["holders"], 4321)
        self.assertEqual(row["vol24h"], 5_000_000)
        old = chain_run.augment(self.conn, _row(vol24h=None), self.now + 10_000, 600)
        self.assertIsNone(old["holders"])


class StubPrefetch:
    def __init__(self):
        self.added = []

    def enqueue(self, address, meta=None, hot=False):
        self.added.append((address, hot))
        return True


class FakeAgg:
    """judge_round が使う集計の最小版（active_since / row だけ）。"""

    def __init__(self, rows):
        self.rows = rows

    def active_since(self, _since):
        return list(self.rows)

    def row(self, address, _now):
        return dict(self.rows[address])

    def features(self, _address, _now, _base=None):
        return None


class TestPrefetch(unittest.TestCase):
    """画像（four.meme）と安全確認（GoPlus）は送信時ではなく、見つけた時点で裏で埋める。"""

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.now = 1_000_000
        self.pre = chain_run.Prefetch("bsc", threading.Event())
        self.url = "https://static.four.meme/market/abc.png"
        # URL が取れたら本体も落としに行くので、外に出ないよう既定で止める
        for target, name, value in ((chain_run.enrich, "fetch_logo_file", None),
                                    (chain_run.enrich.dexscreener, "fetch", ([], 200))):
            patcher = mock.patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_same_address_is_queued_once(self):
        self.assertTrue(self.pre.enqueue("0xtok"))
        self.assertFalse(self.pre.enqueue("0xtok"))
        self.assertEqual(self.pre.queued, 1)
        self.assertEqual(self.pre.queue.qsize(), 1)

    def test_it_can_be_queued_again_on_purpose(self):
        """画像が後から用意される銘柄は、一度処理した後でも積み直せる。"""
        self.assertTrue(self.pre.enqueue("0xtok", hot=True))
        self.assertFalse(self.pre.enqueue("0xtok", hot=True))
        self.assertTrue(self.pre.enqueue("0xtok", hot=True, force=True))
        self.assertEqual(self.pre.hot.qsize(), 2)

    def test_a_scoring_token_without_an_image_is_queued_again(self):
        db.upsert_token(self.conn, "bsc", "0xtok", {"symbol": "T"}, self.now)
        db.save_logo(self.conn, "bsc", "0xtok", None, self.now - 600)   # 探して無かった
        self.conn.commit()
        cfg = json.loads(json.dumps(watch.DEFAULTS))
        self.assertTrue(chain_run.want_logo(self.conn, cfg, "bsc", "0xtok", 72, self.now,
                                            self.pre))
        self.assertEqual(self.pre.hot.qsize(), 1)

    def test_it_is_not_queued_when_the_score_is_low_or_the_image_is_there(self):
        db.upsert_token(self.conn, "bsc", "0xtok", {"symbol": "T"}, self.now)
        self.conn.commit()
        cfg = json.loads(json.dumps(watch.DEFAULTS))
        # db_min_score（既定 40）に届かないうちは聞き直さない
        self.assertFalse(chain_run.want_logo(self.conn, cfg, "bsc", "0xtok", 20, self.now,
                                             self.pre))
        db.save_logo(self.conn, "bsc", "0xtok", "https://img/x.png")
        self.conn.commit()
        self.assertFalse(chain_run.want_logo(self.conn, cfg, "bsc", "0xtok", 72, self.now,
                                             self.pre))
        self.assertEqual(self.pre.hot.qsize(), 0)

    def test_it_stops_after_the_retry_limit(self):
        db.upsert_token(self.conn, "bsc", "0xtok", {"symbol": "T"}, self.now)
        for _ in range(enrich.LOGO_TRIES):
            db.save_logo(self.conn, "bsc", "0xtok", None, self.now - 600)
        self.conn.commit()
        cfg = json.loads(json.dumps(watch.DEFAULTS))
        self.assertFalse(chain_run.want_logo(self.conn, cfg, "bsc", "0xtok", 72, self.now,
                                            self.pre))

    def test_it_fills_the_logo_and_the_security(self):
        db.upsert_token(self.conn, "bsc", "0xtok", {"symbol": "T"}, self.now)
        with mock.patch.object(chain_run.enrich.fourmeme, "image",
                               return_value=self.url) as fm, \
                mock.patch.object(chain_run.enrich.goplus, "fetch",
                                  return_value=({"is_honeypot": "0"}, "ok")) as gp:
            self.assertTrue(self.pre.fill(self.conn, "0xtok", self.now))
        fm.assert_called_once()
        gp.assert_called_once()
        tok = db.get_token(self.conn, "bsc", "0xtok")
        self.assertEqual(tok["logo"], self.url)
        self.assertEqual(json.loads(tok["security_json"])["is_honeypot"], "0")
        self.assertEqual((self.pre.done, self.pre.logo_hits, self.pre.errors), (1, 1, 0))

    def test_a_missing_image_is_remembered(self):
        """画像が無い銘柄は logo_ts だけ入れる（否定キャッシュ＝二度と撃たない）。"""
        db.upsert_token(self.conn, "bsc", "0xtok", {"symbol": "T"}, self.now)
        with mock.patch.object(chain_run.enrich.fourmeme, "image", return_value=None), \
                mock.patch.object(chain_run.enrich.goplus, "fetch",
                                  return_value=(None, "error")):
            self.pre.fill(self.conn, "0xtok", self.now)
        tok = db.get_token(self.conn, "bsc", "0xtok")
        self.assertIsNone(tok["logo"])
        self.assertEqual(tok["logo_ts"], self.now)
        self.assertEqual((self.pre.done, self.pre.logo_hits), (1, 0))

    def test_a_newborn_token_gets_a_minimal_row_and_no_goplus(self):
        """tokens 行が無い生まれたてでも画像を残せる。GoPlus はまだ知らないので聞かない。"""
        meta = {"symbol": "T", "launchpad": "four.meme", "created_ts": self.now - 30}
        with mock.patch.object(chain_run.enrich.fourmeme, "image",
                               return_value=self.url), \
                mock.patch.object(chain_run.enrich.goplus, "fetch") as gp:
            self.pre.fill(self.conn, "0xtok", self.now, meta=meta, hot=True)
        gp.assert_not_called()
        tok = db.get_token(self.conn, "bsc", "0xtok")
        self.assertEqual((tok["logo"], tok["symbol"], tok["launchpad"], tok["creation_ts"]),
                         (self.url, "T", "four.meme", self.now - 30))
        # 判定の基準になる列は最小行では空のまま（M2 の倍率・初認識・更新対象）
        for col in ("first_price", "first_mcap", "first_seen_ts", "last_price",
                    "last_seen_ts", "last_active_ts", "peak_price"):
            self.assertIsNone(tok[col], col)
        self.assertEqual((self.pre.done, self.pre.new_done, self.pre.logo_hits), (1, 1, 1))

    def test_the_minimal_row_does_not_become_the_multiplier_basis(self):
        """最小行を作った後でも、M2 の基準は judge_token が見た最初の価格になる。"""
        self.conn.isolation_level = None
        db.ensure_token(self.conn, "bsc", "0xabc", symbol="T", launchpad="four.meme",
                        creation_ts=self.now - 30)
        self.assertIsNone(db.get_token(self.conn, "bsc", "0xabc")["first_price"])
        with mock.patch.object(chain_run.enrich, "security", return_value=None):
            chain_run.judge_token(self.conn, _cfg(), _row(price=0.001, mcap=1_000_000),
                                  self.now)
            chain_run.judge_token(self.conn, _cfg(), _row(price=0.002, mcap=2_000_000),
                                  self.now + 60)
        tok = db.get_token(self.conn, "bsc", "0xabc")
        self.assertEqual((tok["first_price"], tok["first_mcap"], tok["first_seen_ts"]),
                         (0.001, 1_000_000, self.now))

    def test_a_newborn_token_is_taken_before_the_backlog(self):
        """通常の列が詰まっていても、後から積んだ生まれたての 1 件が次に処理される。"""
        for i in range(100):
            self.pre.enqueue(f"0x{i:03d}")
        self.assertEqual(self.pre.take()[0], "0x000")     # 通常の列は積んだ順
        self.assertTrue(self.pre.enqueue("0xnew", {"launchpad": "four.meme"}, hot=True))
        address, meta, hot = self.pre.take()
        self.assertEqual((address, hot), ("0xnew", True))
        self.assertEqual(meta["launchpad"], "four.meme")
        self.assertEqual(self.pre.take()[0], "0x001")     # その後は通常の列の続き


class TestPrefetchTriggers(unittest.TestCase):
    """積み口は 2 つ: four.meme の初見（resolve）と、フィルタを通った銘柄（judge）。"""

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        from chain import store
        store.ensure(self.conn)
        self.pre = StubPrefetch()

    def _resolver(self):
        return chain_run.Resolver(FakeRpc([]), decode.Decoder(), "bsc", lambda t: None,
                                  chain_run.DEFAULTS, None, self.pre)

    def _meta(self, launchpad):
        return {"decimals": 18, "supply": 1e9, "symbol": "T", "quote": None,
                "launchpad": launchpad, "created_ts": None}

    def test_a_new_fourmeme_token_is_queued_when_it_is_first_seen(self):
        """初見は hot（画像を先に取りに行かせる）で積む。"""
        self._resolver()._save_base(self.conn, "0xtok", self._meta("four.meme"))
        self.assertEqual(self.pre.added, [("0xtok", True)])

    def test_other_launchpads_are_not_queued_from_resolve(self):
        self._resolver()._save_base(self.conn, "0xpcs", self._meta(None))
        self.assertEqual(self.pre.added, [])

    def test_judge_round_queues_what_passes_the_filters(self):
        self.conn.isolation_level = None
        agg = FakeAgg({"0xabc": _row(), "0xsmall": _row(address="0xsmall", liquidity=1)})
        with mock.patch.object(chain_run.enrich, "security", return_value=None):
            chain_run.judge_round(self.conn, _cfg(), agg, chain_run.DEFAULTS, None,
                                  chain_run.Stats(), now=1_000_000, prefetch=self.pre)
        self.assertEqual(self.pre.added, [("0xabc", False)])     # 通常の列（画像＋安全確認）


if __name__ == "__main__":
    unittest.main()
