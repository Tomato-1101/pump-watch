"""盛り上がり確定レーン（hot.py）の判定・確定・本文・日次まとめを検証する。

GMGN も Telegram も呼ばない。GMGN は `hot.gmgn.call` を差し替えたフィクスチャ、
名簿は `hot.load_wallets` の差し替え、送信は cycle に渡す sender で受け取る。
しきい値は実 config.json ではなくここの CFG に固定する（/set で変えても落ちないように）。
"""
import contextlib
import io
import itertools
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import db  # noqa: E402
import hot  # noqa: E402
import notify  # noqa: E402

T0 = 1_789_800_000          # 2026-09-19 15:40 JST
DAY = hot.jst_day(T0)


def hot_cfg(**over):
    c = json.loads(json.dumps(hot.DEFAULTS))
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(c.get(k), dict):
            c[k].update(v)
        else:
            c[k] = v
    return c


def cfg(**over):
    c = {"chains": ["bsc"], "dry_run": True, "hot": hot_cfg()}
    c.update(over)
    return c


def trend(**over):
    """GMGN の trending 24h の行（規模も勢いも通る値）。"""
    r = {"address": "0xAAA111", "symbol": "MEME", "swaps": 12340, "buys": 7100,
         "sells": 5240, "volume": 1_500_000, "holder_count": 2310, "price": 0.001,
         "market_cap": 410000, "liquidity": 86000, "price_change_percent": 480,
         "smart_degen_count": 12, "renowned_count": 3, "hot_level": 3,
         "is_wash_trading": False, "is_honeypot": 0, "bot_degen_rate": 0.63,
         "bundler_rate": 0.12, "rat_trader_amount_rate": 0.03, "launchpad": "flap",
         "open_timestamp": 1_789_758_660, "total_supply": 1_000_000_000,
         "history_highest_market_cap": 612000, "twitter_username": "meme"}
    r.update(over)
    return r


def obs(ts=T0, src="swaps", chain="bsc", **over):
    row = trend(**over)
    return hot.obs_row(chain, str(row["address"]).lower(), row, ts, src)


class FakeGMGN:
    """`gmgn market trending` / `token traders` / `market kline` の差し替え。"""

    def __init__(self, trending=None, traders=None, kline=None, fail=()):
        self.trending = trending or {}
        self.traders = traders or {}
        self.kline = kline if kline is not None else []
        self.fail = set(fail)
        self.calls = []

    def kind(self, args):
        if args[:2] == ["market", "trending"]:
            if "--min-volume" in args:
                return "floor"
            if "--min-price-change-percent" in args:
                return "chg100"
            return args[args.index("--order-by") + 1]
        if args[:2] == ["token", "traders"]:
            return args[args.index("--tag") + 1] if "--tag" in args else "top"
        return "kline"

    def call(self, conn, args):
        args = list(args)
        self.calls.append(args)
        kind = self.kind(args)
        if kind in self.fail:
            raise RuntimeError("boom")
        if kind == "kline":
            return {"list": self.kline}, 0
        if args[:2] == ["token", "traders"]:
            return {"list": self.traders.get(kind, [])}, 0
        return {"data": {"rank": self.trending.get(kind, [])}}, 0


class HotCase(unittest.TestCase):
    """一時ファイルの state.db を使う（:memory: だと 2 接続に分かれないので十分）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hot-test-")
        self.conn = db.connect(os.path.join(self.dir, "state.db"))
        self.sent = []
        self.slept = []

    def tearDown(self):
        self.conn.close()

    def run_cycle(self, fake, conf=None, now=T0, wallets=None, sender=None):
        with mock.patch.object(hot.gmgn, "call", fake.call), \
                mock.patch.object(hot, "load_wallets", return_value=wallets or {}):
            return hot.cycle(self.conn, conf or cfg(), now=now,
                             sleeper=self.slept.append,
                             sender=sender or self.keep, echo=lambda *_: None)

    def keep(self, text):
        """送信の代わり。Telegram が受理したときと同じ真を返す。"""
        self.sent.append(text)
        return True


# --- judge -----------------------------------------------------------------

class TestJudge(unittest.TestCase):
    def setUp(self):
        self.hot = hot_cfg()

    def test_pass(self):
        v = hot.judge(obs(), self.hot)
        self.assertTrue(v["ok"])
        self.assertEqual(v["burst_why"], "chg24")
        self.assertEqual(v["size_miss"], [])

    def test_size_short(self):
        v = hot.judge(obs(holder_count=280), self.hot)
        self.assertFalse(v["ok"])
        self.assertFalse(v["size"])
        self.assertTrue(v["burst"])
        self.assertEqual(v["size_miss"], ["保有者 280"])

    def test_size_short_names_every_missing_part(self):
        v = hot.judge(obs(swaps=10, volume=1000, holder_count=5), self.hot)
        self.assertEqual(v["size_miss"], ["取引 10", "出来高 $1K", "保有者 5"])

    def test_burst_short(self):
        v = hot.judge(obs(price_change_percent=62), self.hot)
        self.assertFalse(v["ok"])
        self.assertTrue(v["size"])
        self.assertFalse(v["burst"])
        self.assertEqual(v["burst_why"], "")

    def test_wash_is_not_hot(self):
        v = hot.judge(obs(is_wash_trading=True), self.hot)
        self.assertFalse(v["ok"])
        self.assertFalse(v["quality"])

    def test_honeypot_is_not_hot(self):
        v = hot.judge(obs(is_honeypot=1), self.hot)
        self.assertFalse(v["ok"])
        self.assertFalse(v["quality"])

    def test_big_swaps_with_small_change(self):
        v = hot.judge(obs(swaps=30000, price_change_percent=20), self.hot)
        self.assertTrue(v["ok"])
        self.assertEqual(v["burst_why"], "big_swaps")

    def test_big_swaps_needs_both(self):
        self.assertFalse(hot.judge(obs(swaps=29999, price_change_percent=20), self.hot)["ok"])
        self.assertFalse(hot.judge(obs(swaps=30000, price_change_percent=19), self.hot)["ok"])

    def test_day_range(self):
        day = {"count": 2, "runup": 2.0, "first_holders": 2310}
        v = hot.judge(obs(price_change_percent=10), self.hot, day)
        self.assertTrue(v["ok"])
        self.assertEqual(v["burst_why"], "day_range")

    def test_day_range_needs_two_observations(self):
        day = {"count": 1, "runup": 2.0, "first_holders": 2310}
        self.assertFalse(hot.judge(obs(price_change_percent=10), self.hot, day)["ok"])

    def test_day_range_needs_24h_not_down(self):
        """値幅で通すときも 24h が day_range_chg24_pct（既定 0）% 以上であること。"""
        day = {"count": 2, "runup": 2.0, "first_holders": 2310}
        self.assertEqual(hot.judge(obs(price_change_percent=0), self.hot, day)["burst_why"],
                         "day_range")
        v = hot.judge(obs(price_change_percent=-5), self.hot, day)
        self.assertFalse(v["burst"])
        self.assertEqual(v["burst_why"], "")

    def test_holder_growth(self):
        """人数 +300・伸び率 20%・24h +20% の 3 つとも境界ちょうどで通る。"""
        day = {"count": 1, "runup": 1.0, "first_holders": 1500}
        v = hot.judge(obs(holder_count=1800, price_change_percent=20), self.hot, day)
        self.assertTrue(v["ok"])
        self.assertEqual(v["burst_why"], "holders")

    def test_holder_growth_short(self):
        day = {"count": 1, "runup": 1.0, "first_holders": 1000}
        self.assertFalse(hot.judge(obs(holder_count=1299, price_change_percent=20),
                                   self.hot, day)["ok"])

    def test_holder_growth_needs_rate(self):
        """+300 人でも伸び率 20% 未満は不可（保有者数万の既存銘柄・株式トークンが横ばいで通っていた）。"""
        day = {"count": 1, "runup": 1.0, "first_holders": 10000}
        v = hot.judge(obs(holder_count=10300, price_change_percent=50), self.hot, day)
        self.assertFalse(v["burst"])
        self.assertEqual(v["burst_why"], "")

    def test_holder_growth_needs_rise(self):
        """伸び率を満たしても 24h が holder_chg24_pct（既定 20）% 未満は不可。"""
        day = {"count": 1, "runup": 1.0, "first_holders": 1000}
        self.assertFalse(hot.judge(obs(holder_count=1500, price_change_percent=19),
                                   self.hot, day)["burst"])
        v = hot.judge(obs(holder_count=1500, price_change_percent=20), self.hot, day)
        self.assertTrue(v["ok"])
        self.assertEqual(v["burst_why"], "holders")

    def test_swaps_alternative_with_many_holders(self):
        """取引が size.swaps 未満でも、保有者 1000 以上かつ取引 1500 以上なら規模あり。"""
        v = hot.judge(obs(swaps=2000, holder_count=1200), self.hot)
        self.assertTrue(v["size"])
        self.assertEqual(v["size_miss"], [])
        self.assertTrue(hot.judge(obs(swaps=1500, holder_count=1000), self.hot)["size"])

    def test_swaps_alternative_needs_both(self):
        self.assertEqual(hot.judge(obs(swaps=2000, holder_count=900), self.hot)["size_miss"],
                         ["取引 2,000"])
        self.assertEqual(hot.judge(obs(swaps=1400, holder_count=5000), self.hot)["size_miss"],
                         ["取引 1,400"])

    def test_missing_new_keys_use_defaults(self):
        """config.json の size / burst に新キーが無くても 0 扱いにせず既定値で判定する。"""
        h = hot_cfg()
        for part, keys in (("size", ("alt_holders", "alt_swaps")),
                           ("burst", ("day_range_chg24_pct", "holder_growth_pct",
                                      "holder_chg24_pct"))):
            for k in keys:
                del h[part][k]
        v = hot.judge(obs(swaps=1000, holder_count=5000), h)
        self.assertFalse(v["size"])
        self.assertEqual(v["size_miss"], ["取引 1,000"])
        day = {"count": 1, "runup": 1.0, "first_holders": 10000}
        self.assertFalse(hot.judge(obs(holder_count=10300, price_change_percent=50), h, day)["burst"])
        day = {"count": 1, "runup": 1.0, "first_holders": 1000}
        self.assertFalse(hot.judge(obs(holder_count=1500, price_change_percent=10), h, day)["burst"])

    def test_multiple_and_peak_are_not_used(self):
        """倍率・史上最高時価総額は表示だけ（judge の結果を動かさない）。"""
        a = hot.judge(obs(history_highest_market_cap=0), self.hot)
        b = hot.judge(obs(history_highest_market_cap=99_000_000), self.hot)
        self.assertEqual(a, b)

    def test_peak_floor(self):
        """他の条件を全部満たしていても、ピークが size.peak_mcap_usd 未満なら規模不足。"""
        v = hot.judge(obs(market_cap=90000), self.hot)
        self.assertFalse(v["ok"])
        self.assertFalse(v["size"])
        self.assertIn("ピーク時価総額 $90K", v["size_miss"])

    def test_peak_floor_uses_the_days_peak(self):
        """下限はその日のピーク（peak_mc）で見る。今の時価総額が低くても day のピークが高ければ通る。"""
        day = {"peak_mc": 150000}
        v = hot.judge(obs(market_cap=40000), self.hot, day)
        self.assertTrue(v["size"])
        self.assertEqual(v["size_miss"], [])

    def test_peak_floor_without_day_uses_row_market_cap(self):
        """day が無い（cmd_judge 等）ときは行の market_cap をそのままピークとして使う。"""
        self.assertTrue(hot.judge(obs(market_cap=120000), self.hot)["size"])
        self.assertFalse(hot.judge(obs(market_cap=80000), self.hot)["size"])

    def test_volume_alternative(self):
        """出来高が薄くても、保有者・取引が alt_swaps 並に多くピークも大きければ規模あり。"""
        v = hot.judge(obs(volume=200000, holder_count=1200, swaps=1600, market_cap=350000),
                      self.hot)
        self.assertTrue(v["size"])
        self.assertEqual(v["size_miss"], [])

    def test_volume_alternative_uses_the_days_peak(self):
        """代替のピークも今の時価総額ではなくその日のピークで見る（玉兔・币安协议・八仙はこの経路で確定した）。"""
        row = obs(volume=200000, holder_count=1200, swaps=1600, market_cap=200000)
        self.assertFalse(hot.judge(row, self.hot)["size"])
        self.assertTrue(hot.judge(row, self.hot, {"peak_mc": 320000})["size"])

    def test_volume_alternative_needs_all_four(self):
        """ピーク・出来高・保有者・取引のどれか 1 つだけ足りなくても代替は成り立たず、出来高は size.volume_usd で測る。"""
        base = dict(volume=200000, holder_count=1200, swaps=1600, market_cap=350000)
        variants = ({"market_cap": 250000}, {"volume": 140000},
                    {"holder_count": 900}, {"swaps": 1400},
                    {"holder_count": 900, "swaps": 3000})   # 取引は通常で足りても保有者が少なければ代替なし
        for change in variants:
            row = dict(base, **change)
            with self.subTest(change=change):
                v = hot.judge(obs(**row), self.hot)
                self.assertFalse(v["size"])
                self.assertIn(f"出来高 {hot.rules.fmt_usd(row['volume'])}", v["size_miss"])

    def test_peak_floor_is_inclusive(self):
        """ちょうど size.peak_mcap_usd なら下限を満たす。"""
        self.assertTrue(hot.judge(obs(market_cap=100000), self.hot)["size"])

    def test_peak_floor_missing_config_key_uses_default(self):
        """config.json の size に peak_mcap_usd 等の新キーが無くても既定値(10 万)で下限が効く。"""
        h = hot_cfg()
        for k in ("peak_mcap_usd", "alt_volume_usd", "alt_peak_mcap_usd"):
            del h["size"][k]
        self.assertFalse(hot.judge(obs(market_cap=90000), h)["size"])
        self.assertTrue(hot.judge(obs(market_cap=150000), h)["size"])


class TestNearRank(unittest.TestCase):
    """惜しい順の達成度（hot._near_rank）。judge の代替ルートと揃っているかを検算する。"""

    def setUp(self):
        self.hot = hot_cfg()

    def test_peak_achievement_is_in_the_minimum(self):
        """規模不足で出来高・取引・保有者の達成度が全部 1 以上でも、ピークが 0.9 なら 0.9 になる。"""
        row = obs(swaps=3000, volume=300000, holder_count=300, market_cap=90000)
        v = hot.judge(row, self.hot)
        self.assertFalse(v["size"])
        self.assertAlmostEqual(hot._near_rank(row, v, self.hot), 0.9)

    def test_alt_swaps_without_alt_peak(self):
        """ピークが alt_peak_mcap_usd 未満なら、取引は代替で足りても出来高は size.volume_usd で測る（264K/300K）。"""
        row = obs(volume=264000, swaps=1600, holder_count=1200, market_cap=250000)
        v = hot.judge(row, self.hot)
        self.assertAlmostEqual(hot._near_rank(row, v, self.hot), 0.88)

    def test_swaps_just_below_alt_swaps(self):
        """取引が alt_swaps に少し足りないだけなら、出来高の代替が成り立つ前提で近さを測る（1400/1500）。"""
        row = obs(volume=160000, swaps=1400, holder_count=2000, market_cap=400000)
        v = hot.judge(row, self.hot)
        self.assertFalse(v["size"])
        self.assertAlmostEqual(hot._near_rank(row, v, self.hot), 1400 / 1500)

    def test_stricter_alt_swaps_is_not_counted_as_met(self):
        """alt_swaps を size.swaps より厳しくしたら、出来高の代替には alt_swaps まで要る（3000/4000）。"""
        h = hot_cfg(size={"alt_swaps": 4000})
        row = obs(volume=150000, swaps=3000, holder_count=1000, market_cap=320000)
        v = hot.judge(row, h, {"peak_mc": 320000})
        self.assertFalse(v["size"])
        self.assertAlmostEqual(hot._near_rank(row, v, h), 0.75)

    def test_size_miss_is_always_below_one(self):
        """規模不足の行の達成度は、設定を変えても必ず 1 未満（judge と食い違わない）。"""
        grid = itertools.product((140000, 150000, 200000, 300000, 450000), (1400, 1500, 3000, 4000),
                                 (250, 900, 1000, 2000), (90000, 100000, 250000, 300000, 400000))
        rows = [obs(volume=vol, swaps=sw, holder_count=hd, market_cap=mc)
                for vol, sw, hd, mc in grid]
        for size in ({}, {"alt_swaps": 4000}, {"alt_volume_usd": 400000}, {"alt_holders": 200}):
            h = hot_cfg(size=size)
            for row in rows:
                v = hot.judge(row, h)
                if not v["size"]:
                    with self.subTest(size=size, row=(row["volume"], row["swaps"], row["holders"],
                                                      row["market_cap"])):
                        self.assertLess(hot._near_rank(row, v, h), 1)

    def test_volume_alternative_rate(self):
        """代替の前提（保有者・取引・ピーク）を満たす行は、出来高の達成度を alt_volume_usd 基準で測る。"""
        row = obs(holder_count=1000, swaps=1500, volume=135000, market_cap=300000)
        v = hot.judge(row, self.hot)
        self.assertFalse(v["size"])
        self.assertAlmostEqual(hot._near_rank(row, v, self.hot), 0.9)


# --- 取得と観測 --------------------------------------------------------------

class TestObsRow(unittest.TestCase):
    def test_price_change_percent_is_percent(self):
        """100 = +100%。単位変換をしない（GMGN の実測どおり）。"""
        self.assertEqual(obs(price_change_percent=-27.8828)["chg24"], -27.8828)
        self.assertEqual(obs(price_change_percent="480")["chg24"], 480.0)

    def test_missing_fields_become_zero(self):
        row = hot.obs_row("bsc", "0xa", {"address": "0xa"}, T0, "swaps")
        self.assertEqual(row["swaps"], 0)
        self.assertEqual(row["volume"], 0.0)
        self.assertEqual(row["symbol"], "")

    def test_open_ts_falls_back_to_creation(self):
        row = obs(open_timestamp=0, creation_timestamp=1_789_700_000)
        self.assertEqual(row["open_ts"], 1_789_700_000)


class TestFetchPool(HotCase):
    def test_union_and_src(self):
        a, b = trend(address="0xAAA"), trend(address="0xBBB", symbol="BBB")
        fake = FakeGMGN(trending={"swaps": [a, b], "volume": [a], "chg100": [a]})
        with mock.patch.object(hot.gmgn, "call", fake.call):
            pool = hot.fetch_pool(self.conn, cfg(), ["bsc"], T0,
                                  hot.Pacer(0, sleeper=self.slept.append),
                                  {"calls": 0, "errors": 0})
        self.assertEqual(sorted(k[1] for k in pool), ["0xaaa", "0xbbb"])
        self.assertEqual(pool[("bsc", "0xaaa")]["src"], "swaps,volume,chg100")
        self.assertEqual(pool[("bsc", "0xbbb")]["src"], "swaps")

    def test_chg100_uses_min_price_change_percent(self):
        args = hot.pull_args("bsc", "chg100")
        self.assertIn("--min-price-change-percent", args)
        self.assertEqual(args[args.index("--min-price-change-percent") + 1], "100")
        self.assertEqual(args[args.index("--order-by") + 1], "volume")

    def test_floor_uses_loosest_size_and_burst(self):
        """floor は代替ルート込みで最も緩い規模の下限と、勢いの 24h 変化率の最小で絞る。"""
        args = hot.pull_args("bsc", "floor", hot=hot_cfg())
        val = lambda k: args[args.index(k) + 1]    # noqa: E731
        self.assertEqual(val("--order-by"), "volume")
        self.assertEqual(val("--min-volume"), "150000")
        self.assertEqual(val("--min-holder-count"), "300")
        self.assertEqual(val("--min-swaps"), "1500")
        self.assertEqual(val("--min-price-change-percent"), "0")

    def test_floor_follows_config(self):
        h = hot_cfg(size={"alt_swaps": 4000, "alt_volume_usd": 400000},
                    burst={"day_range_chg24_pct": -10})
        args = hot.pull_args("bsc", "floor", hot=h)
        val = lambda k: args[args.index(k) + 1]    # noqa: E731
        self.assertEqual(val("--min-swaps"), "3000")
        self.assertEqual(val("--min-volume"), "300000")
        self.assertEqual(val("--min-price-change-percent"), "-10")

    def test_floor_is_in_default_pulls(self):
        self.assertIn("floor", hot.DEFAULTS["pulls"])

    def test_fetch_pool_passes_config_to_floor(self):
        h = hot_cfg(size={"alt_volume_usd": 200000, "volume_usd": 250000})
        fake = FakeGMGN(trending={})
        with mock.patch.object(hot.gmgn, "call", fake.call):
            hot.fetch_pool(self.conn, cfg(hot=h), ["bsc"], T0,
                           hot.Pacer(0, sleeper=self.slept.append), {"calls": 0, "errors": 0})
        floor = [a for a in fake.calls if "--min-volume" in a]
        self.assertEqual(len(floor), 1)
        self.assertEqual(floor[0][floor[0].index("--min-volume") + 1], "200000")

    def test_full_floor_is_logged(self):
        """floor が上限件数で返ったら取りこぼしの可能性をログに残す（ほかの並びは常に満杯なので出さない）。"""
        rows = [trend(address=f"0x{i:040x}") for i in range(hot.PULL_LIMIT)]
        out = io.StringIO()
        with mock.patch.object(hot.gmgn, "call", FakeGMGN(trending={"floor": rows, "swaps": rows}).call), \
                contextlib.redirect_stdout(out):
            hot.fetch_pool(self.conn, cfg(), ["bsc"], T0,
                           hot.Pacer(0, sleeper=self.slept.append), {"calls": 0, "errors": 0})
        self.assertEqual(out.getvalue().count("上限"), 1)
        self.assertIn("floor", out.getvalue())

    def test_empty_chain_is_skipped(self):
        fake = FakeGMGN(trending={})
        with mock.patch.object(hot.gmgn, "call", fake.call):
            pool = hot.fetch_pool(self.conn, cfg(), ["arc"], T0,
                                  hot.Pacer(0, sleeper=self.slept.append),
                                  {"calls": 0, "errors": 0})
        self.assertEqual(pool, {})

    def test_failure_retries_once_then_gives_up(self):
        fake = FakeGMGN(trending={"swaps": [trend()]}, fail=("volume",))
        stats = {"calls": 0, "errors": 0}
        with mock.patch.object(hot.gmgn, "call", fake.call):
            pool = hot.fetch_pool(self.conn, cfg(), ["bsc"], T0,
                                  hot.Pacer(0, sleeper=self.slept.append), stats)
        self.assertEqual(len(pool), 1)
        self.assertEqual(stats["errors"], 2)
        self.assertIn(hot.RETRY_WAIT, self.slept)


class TestDayStats(HotCase):
    """runup（安値→その後の高値）を一時 DB の観測から検算する。"""

    def save_prices(self, address, prices, **over):
        hot.save_obs(self.conn, [obs(ts=T0 + i * 600, address=address, price=p, **over)
                                 for i, p in enumerate(prices)])

    def stats(self):
        return hot.day_stats(self.conn, hot.jst_day_start(T0), T0 + 3600)

    def judge_latest(self, address):
        start = hot.jst_day_start(T0)
        row = [r for r in hot.latest_obs(self.conn, start, T0 + 3600)
               if r["address"] == address][0]
        return hot.judge(row, hot_cfg(), self.stats()[("bsc", address)])

    def test_runup(self):
        self.save_prices("0xr1", [1, 3, 0.5, 0.8])
        self.save_prices("0xr2", [3, 1, 2.5])
        self.save_prices("0xr3", [0, 0])
        st = self.stats()
        self.assertAlmostEqual(st[("bsc", "0xr1")]["runup"], 3.0)
        self.assertAlmostEqual(st[("bsc", "0xr2")]["runup"], 2.5)
        self.assertIsNone(st[("bsc", "0xr3")]["runup"])
        self.assertEqual(st[("bsc", "0xr1")]["count"], 4)
        self.assertNotIn("min_price", st[("bsc", "0xr1")])

    def test_crash_is_not_day_range(self):
        """高値→安値（最高値÷最安値なら 4 倍）は値幅に数えない。"""
        self.save_prices("0xc1", [0.004, 0.001], price_change_percent=10)
        self.assertAlmostEqual(self.stats()[("bsc", "0xc1")]["runup"], 1.0)
        v = self.judge_latest("0xc1")
        self.assertEqual(v["burst_why"], "")
        self.assertFalse(v["ok"])

    def test_rise_is_day_range_unless_24h_is_down(self):
        self.save_prices("0xu1", [0.001, 0.0025], price_change_percent=0)
        self.assertEqual(self.judge_latest("0xu1")["burst_why"], "day_range")
        self.save_prices("0xd1", [0.001, 0.0025], price_change_percent=-1)
        self.assertEqual(self.judge_latest("0xd1")["burst_why"], "")

    def save_series(self, address, prices, start, step=600, **over):
        hot.save_obs(self.conn, [obs(ts=start + i * step, address=address, price=p, **over)
                                 for i, p in enumerate(prices)])

    def test_flat_24h_cancels_burst(self):
        """直近 24h がほぼ横ばいなら、GMGN の 24h 変化率が +302% でも勢いにしない（DAI 09-29）。"""
        until = T0
        self.save_series("0xf1", [1.0, 1.001, 0.999] * 50, until - 86400 + 60,
                         price_change_percent=302)
        st = hot.day_stats(self.conn, hot.jst_day_start(until), until)[("bsc", "0xf1")]
        self.assertTrue(st["flat"])
        row = [r for r in hot.latest_obs(self.conn, hot.jst_day_start(until), until)
               if r["address"] == "0xf1"][0]
        v = hot.judge(row, hot_cfg(), st)
        self.assertEqual(v["burst_why"], "")
        self.assertFalse(v["ok"])
        self.assertTrue(v["flat"])

    def test_glitch_low_does_not_break_flat(self):
        """一瞬の 0.5→0.12 ドル表示（前後は 1 ドル）は外れ値として除き、値幅 8 倍の確定にしない（DAI 09-28）。"""
        until = T0
        prices = [1.0] * 20 + [0.5, 0.5, 0.12] + [1.0] * 120
        self.save_series("0xf2", prices, until - 600 * (len(prices) - 1), price_change_percent=0)
        st = hot.day_stats(self.conn, hot.jst_day_start(until), until)[("bsc", "0xf2")]
        self.assertTrue(st["flat"])
        row = [r for r in hot.latest_obs(self.conn, hot.jst_day_start(until), until)
               if r["address"] == "0xf2"][0]
        self.assertEqual(hot.judge(row, hot_cfg(), st)["burst_why"], "")

    def test_glitch_at_window_start_uses_lookback(self):
        """外れ値が 24h 窓のちょうど先頭にあっても、窓の手前の観測で挟んで除く。"""
        until = T0
        start = until - 86400 + 60
        hot.save_obs(self.conn, [obs(ts=start - 1200, address="0xf3", price=1.0),
                                 obs(ts=start - 600, address="0xf3", price=1.0)])
        self.save_series("0xf3", [0.12, 0.5] + [1.0] * 140, start)
        st = hot.day_stats(self.conn, hot.jst_day_start(until), until)[("bsc", "0xf3")]
        self.assertTrue(st["flat"])

    def test_rise_is_not_flat(self):
        until = T0
        self.save_series("0xf4", [1.0] * 100 + [1.2, 1.5, 2.0], until - 600 * 102)
        st = hot.day_stats(self.conn, hot.jst_day_start(until), until)[("bsc", "0xf4")]
        self.assertFalse(st["flat"])

    def test_short_history_is_not_flat(self):
        """来たばかり（最初の観測から 12h 未満）の銘柄は、上がった後しか見ていないので横ばいと言わない。"""
        until = T0
        self.save_series("0xf5", [1.0] * 60, until - 600 * 59)
        st = hot.day_stats(self.conn, hot.jst_day_start(until), until)[("bsc", "0xf5")]
        self.assertFalse(st["flat"])
        self.assertTrue(self.judge_latest("0xf5")["burst"])

    def test_gapped_v_is_not_glitch(self):
        """観測が飛んだ本物の V 字（-22h→-8h の間に暴落、-2h に全戻し）は外れ値として除かず、値幅で通す。"""
        until = T0
        pts = [(-23, 1.0), (-22.5, 1.0), (-22, 1.0), (-8, 0.3), (-7, 0.3),
               (-2, 1.0), (-1.5, 1.0), (-1, 1.0), (-0.5, 1.0), (0, 1.02)]
        hot.save_obs(self.conn, [obs(ts=until + int(h * 3600), address="0xf7", price=p, price_change_percent=2)
                                 for h, p in pts])
        st = hot.day_stats(self.conn, hot.jst_day_start(until), until)[("bsc", "0xf7")]
        self.assertFalse(st["flat"])

    def test_flat_is_not_near_in_digest(self):
        """横ばいで勢いを消した銘柄は、24h 変化率が大きくても「惜しい」に出さない。"""
        until = T0
        self.save_series("0xf8", [1.0, 1.001, 0.999] * 50, until - 86400 + 60,
                         price_change_percent=302, symbol="STABLE")
        text = hot.digest_text(self.conn, cfg(), until)
        self.assertNotIn("STABLE", text)
        self.assertIn("惜しい（規模か勢いの片方だけ）:\n- なし", text)

    def test_upward_spike_keeps_flat_off(self):
        """上に跳ねる外れ値は除かない（横ばいと言えなくなるだけで、確定を取り消す向きには働かない）。"""
        until = T0
        prices = [1.0] * 70 + [3.0] + [1.0] * 70
        self.save_series("0xf6", prices, until - 600 * (len(prices) - 1))
        st = hot.day_stats(self.conn, hot.jst_day_start(until), until)[("bsc", "0xf6")]
        self.assertFalse(st["flat"])

    def test_peak_mc_is_max_within_range_only(self):
        """peak_mc は [start, until] の観測だけの market_cap 最大値(範囲外は無視する)。"""
        start = hot.jst_day_start(T0)
        until = T0 + 3600
        hot.save_obs(self.conn, [
            obs(ts=start - 600, address="0xpk1", market_cap=999999),   # 範囲より前(除外)
            obs(ts=T0, address="0xpk1", market_cap=50000),
            obs(ts=T0 + 60, address="0xpk1", market_cap=150000),       # 範囲内の最大
            obs(ts=until + 600, address="0xpk1", market_cap=888888),   # 範囲より後(除外)
        ])
        st = hot.day_stats(self.conn, start, until)
        self.assertAlmostEqual(st[("bsc", "0xpk1")]["peak_mc"], 150000)


# --- 確定（連続通過・クールダウン・同日 1 行） --------------------------------

class TestConfirm(HotCase):
    def fake(self, **over):
        return FakeGMGN(trending={"swaps": [trend(**over)]})

    def test_one_pass_is_not_enough(self):
        res = self.run_cycle(self.fake())
        self.assertEqual((res["pool"], res["hit"]), (1, 0))
        self.assertEqual(self.sent, [])

    def test_two_passes_confirm(self):
        self.run_cycle(self.fake())
        res = self.run_cycle(self.fake(), now=T0 + 600)
        self.assertEqual(res["hit"], 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("【盛り上がり】MEME（BSC）", self.sent[0])
        row = self.conn.execute("SELECT * FROM hot_hits").fetchone()
        self.assertEqual((row["day_jst"], row["sent"]), (DAY, 1))

    def test_a_miss_resets_the_streak(self):
        self.run_cycle(self.fake())
        self.run_cycle(self.fake(price_change_percent=10), now=T0 + 600)
        self.assertEqual(self.conn.execute(
            "SELECT streak FROM hot_state").fetchone()["streak"], 0)
        res = self.run_cycle(self.fake(), now=T0 + 1200)
        self.assertEqual(res["hit"], 0)

    def test_leaving_the_pool_resets_the_streak(self):
        self.run_cycle(self.fake())
        self.run_cycle(FakeGMGN(trending={"swaps": []}), now=T0 + 600)
        self.assertEqual(self.conn.execute(
            "SELECT streak FROM hot_state").fetchone()["streak"], 0)

    def test_cooldown_blocks_the_second_notification(self):
        self.run_cycle(self.fake())
        self.run_cycle(self.fake(), now=T0 + 600)
        res = self.run_cycle(self.fake(), now=T0 + 1200)
        self.assertEqual(res["hit"], 0)
        self.assertEqual(len(self.sent), 1)

    def test_cooldown_over_sends_again_the_next_day(self):
        self.run_cycle(self.fake())
        self.run_cycle(self.fake(), now=T0 + 600)
        later = T0 + 49 * 3600
        self.run_cycle(self.fake(), now=later)
        self.assertEqual(len(self.sent), 2)
        days = [r["day_jst"] for r in self.conn.execute(
            "SELECT day_jst FROM hot_hits ORDER BY id")]
        self.assertEqual(days, [DAY, hot.jst_day(later)])

    def test_only_one_row_per_day(self):
        """クールダウンを 0 にしても同じ JST 日に 2 行にはならない（通知も 1 通）。"""
        conf = cfg(hot=hot_cfg(cooldown_hours=0))
        self.run_cycle(self.fake(), conf=conf)
        self.run_cycle(self.fake(), conf=conf, now=T0 + 600)
        self.run_cycle(self.fake(), conf=conf, now=T0 + 1200)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM hot_hits").fetchone()["n"], 1)
        self.assertEqual(len(self.sent), 1)

    def test_dry_run_does_not_send(self):
        self.run_cycle(self.fake())
        with mock.patch.object(hot.gmgn, "call", self.fake().call), \
                mock.patch.object(hot, "load_wallets", return_value={}), \
                mock.patch.object(hot, "telegram_sender") as ts:
            echoed = []
            hot.cycle(self.conn, cfg(), now=T0 + 600, sleeper=self.slept.append,
                      echo=echoed.append)
        ts.assert_not_called()
        self.assertTrue(any("【盛り上がり】" in t for t in echoed))
        self.assertEqual(self.conn.execute(
            "SELECT sent FROM hot_hits").fetchone()["sent"], 0)

    def test_observations_are_recorded_every_cycle(self):
        self.run_cycle(self.fake())
        self.run_cycle(self.fake(), now=T0 + 600)
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM hot_obs ORDER BY ts")]
        self.assertEqual([r["ts"] for r in rows], [T0, T0 + 600])
        self.assertEqual(rows[0]["chain"], "bsc")

    def test_confirmed_hit_fetches_wallets_and_kline(self):
        fake = self.fake()
        fake.traders = {"top": [{"address": "0xF1", "name": "Alice", "tags": [],
                                 "buy_volume_cur": 12000, "realized_profit": 3400}]}
        fake.kline = [{"time": 1, "low": "0.001", "high": "0.0061"}]
        self.run_cycle(self.fake())
        self.run_cycle(fake, now=T0 + 600, wallets={"0xf1": {"name": "Alice", "handle": "ALICE"}})
        kinds = [fake.kind(a) for a in fake.calls]
        self.assertEqual(kinds[-4:], ["top", "smart_degen", "renowned", "kline"])
        self.assertIn("- Alice @ALICE [名簿] 買 $12K / 損益 +$3.4K", self.sent[0])


# --- 有名ウォレット ----------------------------------------------------------

class TestFamousWallets(unittest.TestCase):
    def setUp(self):
        self.hot = hot_cfg()

    def test_tag_match(self):
        rows = [{"address": "0x1", "tags": ["smart_degen"], "buy_volume_cur": 513,
                 "realized_profit": 46}]
        out = hot.famous_wallets(rows, {}, self.hot)
        self.assertEqual(out[0]["kind"], "smart")
        self.assertEqual(out[0]["name"], "0x1")

    def test_kol_and_renowned_share_a_label(self):
        rows = [{"address": "0x1", "tags": ["kol"], "name": "A"},
                {"address": "0x2", "tags": ["renowned"], "name": "B"}]
        self.assertEqual([w["kind"] for w in hot.famous_wallets(rows, {}, self.hot)],
                         ["KOL", "KOL"])

    def test_listed_matches_mixed_case_address(self):
        rows = [{"address": "0xAbCdEf0123", "tags": [], "buy_volume_cur": 1}]
        out = hot.famous_wallets(rows, {"0xabcdef0123": {"name": "Alice", "handle": "x"}},
                                 self.hot)
        self.assertEqual((out[0]["kind"], out[0]["name"], out[0]["listed"]),
                         ("名簿", "Alice", True))

    def test_excluded_tags_are_dropped(self):
        rows = [{"address": "0x1", "tags": ["kol", "dex_bot"]},
                {"address": "0x2", "tags": ["smart_degen", "sandwich_bot"]}]
        self.assertEqual(hot.famous_wallets(rows, {}, self.hot), [])

    def test_unknown_wallets_are_dropped(self):
        self.assertEqual(hot.famous_wallets([{"address": "0x1", "tags": []}], {}, self.hot), [])

    def test_empty_list(self):
        self.assertEqual(hot.famous_wallets([], {"0x1": {"name": "a"}}, self.hot), [])

    def test_order_is_listed_then_buy_volume(self):
        rows = [{"address": "0x1", "tags": ["kol"], "buy_volume_cur": 50},
                {"address": "0x2", "tags": ["kol"], "buy_volume_cur": 500},
                {"address": "0x3", "tags": [], "buy_volume_cur": 5}]
        out = hot.famous_wallets(rows, {"0x3": {"name": "名簿の人"}}, self.hot)
        self.assertEqual([w["address"] for w in out], ["0x3", "0x2", "0x1"])

    def test_at_most_ten_rows(self):
        rows = [{"address": f"0x{i}", "tags": ["kol"], "buy_volume_cur": i} for i in range(30)]
        self.assertEqual(len(hot.famous_wallets(rows, {}, self.hot)), hot.MAX_WALLETS)

    def test_traders_rows_accepts_both_shapes(self):
        self.assertEqual(len(hot.traders_rows({"list": [{"address": "0x1"}]})), 1)
        self.assertEqual(len(hot.traders_rows({"data": {"list": [{"address": "0x1"}]}})), 1)
        self.assertEqual(hot.traders_rows({"list": []}), [])
        self.assertEqual(hot.traders_rows(None), [])


# --- 本文 -------------------------------------------------------------------

class TestMessage(unittest.TestCase):
    def setUp(self):
        self.row = obs()
        self.day = hot.kline_day([{"low": "0.001", "high": "0.0061"}], self.row["total_supply"])
        self.wallets = [
            {"address": "0xf1", "name": "Alice🔶BNB", "twitter": "alicetrades", "kind": "名簿",
             "listed": True, "buy": 12000, "profit": 3400},
            {"address": "0xa8", "name": "BobKOL", "twitter": "bobkol",
             "kind": "KOL", "listed": False, "buy": 2209, "profit": -455},
            {"address": "0xc0ffee01aa", "name": "0xc0ffee01", "twitter": "", "kind": "smart",
             "listed": False, "buy": 513, "profit": 46},
        ]

    def test_every_line(self):
        text = hot.build_message(self.row, self.wallets, self.day)
        self.assertEqual(text.split("\n"), [
            "【盛り上がり】MEME（BSC）",
            "24h: 出来高 $1.5M / 取引 12,340 回（買 7,100・売 5,240） / 保有者 2,310",
            "価格 24h +480%（5.8x）",
            "今日: 安値→高値 6.1x・高値時の時価総額 $6.1M",
            "時価総額 $410K（史上最高 $612K）",
            "上場 2026-09-19 04:11 JST（flap）・流動性 $86K",
            "",
            "有名ウォレット（買い手上位 100 ＋ タグ検索から）:",
            "- Alice🔶BNB @alicetrades [名簿] 買 $12K / 損益 +$3.4K",
            "- BobKOL @bobkol [KOL] 買 $2.2K / 損益 −$455",
            "- 0xc0ffee01 [smart] 買 $513 / 損益 +$46",
            "",
            "注意: bot 比率 63%・バンドル 12%・ネズミ 3%",
            "https://gmgn.ai/bsc/token/0xaaa111",
        ])

    def test_kline_failure(self):
        text = hot.build_message(self.row, self.wallets, None, kline_failed=True)
        self.assertIn("今日: 取得失敗", text)
        self.assertNotIn("安値→高値", text)

    def test_no_famous_wallets(self):
        text = hot.build_message(self.row, [], self.day)
        self.assertIn("有名ウォレット: 該当なし", text)
        self.assertNotIn("買い手上位", text)

    def test_warning_line_is_dropped_when_all_zero(self):
        row = obs(bot_degen_rate=0, bundler_rate=0, rat_trader_amount_rate=0)
        text = hot.build_message(row, [], self.day)
        self.assertNotIn("注意:", text)
        self.assertTrue(text.endswith("https://gmgn.ai/bsc/token/0xaaa111"))

    def test_no_ath_no_parentheses(self):
        text = hot.build_message(obs(history_highest_market_cap=0), [], self.day)
        self.assertIn("時価総額 $410K\n", text)
        self.assertNotIn("史上最高", text)

    def test_unlisted_token_shows_only_liquidity(self):
        text = hot.build_message(obs(open_timestamp=0, creation_timestamp=0), [], self.day)
        self.assertIn("流動性 $86K", text)
        self.assertNotIn("上場", text)

    def test_no_total_supply_drops_the_peak_mcap(self):
        row = obs(total_supply=0)
        day = hot.kline_day([{"low": "0.001", "high": "0.0061"}], row["total_supply"])
        self.assertIn("今日: 安値→高値 6.1x\n", hot.build_message(row, [], day))

    def test_negative_change_has_no_multiple(self):
        self.assertIn("価格 24h -30%\n", hot.build_message(
            obs(price_change_percent=-30), [], self.day))

    def test_no_markdown_or_buttons(self):
        text = hot.build_message(self.row, self.wallets, self.day)
        for mark in ("*", "_", "`", "[点", "点の内訳"):
            self.assertNotIn(mark, text)

    def test_long_message_is_split(self):
        wallets = [dict(self.wallets[0], address=f"0x{i}", name=f"だれか{i}" * 3)
                   for i in range(400)]
        text = hot.build_message(self.row, wallets, self.day)
        chunks = notify.split_message(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= notify.MAX_LEN for c in chunks))
        self.assertEqual("\n".join(chunks), text)

    def test_symbol_control_characters_are_stripped(self):
        text = hot.build_message(obs(symbol="ME‮ME"), [], self.day)
        self.assertTrue(text.startswith("【盛り上がり】MEME（BSC）"))

    def test_kline_day(self):
        d = hot.kline_day([{"low": "2", "high": "3"}, {"low": "1", "high": "5"}], 10)
        self.assertEqual((d["low"], d["high"], d["x"], d["mcap"]), (1.0, 5.0, 5.0, 50.0))
        self.assertIsNone(hot.kline_day([], 10))
        self.assertIsNone(hot.kline_day([{"low": 0, "high": 0}], 10))


# --- 日次まとめ --------------------------------------------------------------

class TestDigest(HotCase):
    def test_no_confirmation(self):
        """hits を渡さない呼び出しは /hot（今日の確定）になったので見出しに「以降」は付かない。
        起点の時刻を出すのは hits を渡すまとめ（cmd_digest と同じ digest_hits 経由）だけ。"""
        text = hot.digest_text(self.conn, cfg(), T0)
        self.assertEqual(text.split("\n")[0], f"【今日の盛り上がり】{DAY}（BSC）確定 0 件")
        self.assertIn("惜しい（規模か勢いの片方だけ）:\n- なし", text)
        self.assertIn("巡回 0 回・GMGN 呼び出し 0 回・エラー 0 回", text)
        hits, _ = hot.digest_hits(self.conn, T0)
        self.assertEqual(hot.digest_text(self.conn, cfg(), T0, hits).split("\n")[0],
                         f"【今日の盛り上がり】{DAY}（BSC）確定 0 件（09-18 15:40 以降）")

    def test_confirmed_items(self):
        fake = FakeGMGN(trending={"swaps": [trend()]})
        self.run_cycle(fake)
        self.run_cycle(fake, now=T0 + 600)
        text = hot.digest_text(self.conn, cfg(), T0 + 900)
        self.assertIn("確定 1 件", text)
        self.assertIn("1. MEME 出来高 $1.5M / 取引 12,340 / 保有者 2,310 / 24h +480%"
                      " / 有名 0 / 通知 15:50", text)
        self.assertIn("巡回 2 回・GMGN 呼び出し 12 回・エラー 0 回", text)

    def test_unsent_confirmation_is_marked(self):
        """dry_run 中（送信なし）の確定は「通知 時刻」でなく「未送信」と出す。"""
        fake = FakeGMGN(trending={"swaps": [trend()]})
        self.run_cycle(fake, sender=lambda text: False)
        self.run_cycle(fake, now=T0 + 600, sender=lambda text: False)
        text = hot.digest_text(self.conn, cfg(), T0 + 900)
        self.assertIn("確定 1 件", text)
        self.assertIn("/ 有名 0 / 通知 未送信", text)
        self.assertNotIn("通知 15:50", text)

    def test_continued_from_a_previous_day(self):
        """クールダウン中で通知しなかったが今日も通過している銘柄は「継続」に出す。"""
        hot.save_obs(self.conn, [obs()])
        self.conn.execute("INSERT INTO hot_state(chain,address,streak,last_pass_ts,"
                          "last_notified_ts) VALUES('bsc','0xaaa111',3,?,?)",
                          (T0, T0 - 20 * 3600))
        text = hot.digest_text(self.conn, cfg(), T0 + 60)
        self.assertIn("（継続）MEME 出来高 $1.5M / 取引 12,340 / 保有者 2,310 / 24h +480%"
                      " / 前日確定・再通知なし", text)

    def test_confirmed_today_is_not_also_continued(self):
        fake = FakeGMGN(trending={"swaps": [trend()]})
        self.run_cycle(fake)
        self.run_cycle(fake, now=T0 + 600)
        self.assertNotIn("（継続）", hot.digest_text(self.conn, cfg(), T0 + 900))

    def test_near_misses(self):
        hot.save_obs(self.conn, [
            obs(address="0xS1", symbol="STORM", volume=420000, swaps=4100,
                holder_count=610, price_change_percent=62),
            obs(address="0xU1", symbol="UrAnus", volume=300000, swaps=3000,
                holder_count=280, price_change_percent=300)])
        text = hot.digest_text(self.conn, cfg(), T0 + 60)
        self.assertIn("- STORM 出来高 $420K / 取引 4,100 / 保有者 610 / 24h +62%"
                      " → 勢い不足", text)
        self.assertIn("- UrAnus 出来高 $300K / 取引 3,000 / 保有者 280 / 24h +300%"
                      " → 規模不足（保有者 280）", text)

    def test_near_limit(self):
        hot.save_obs(self.conn, [obs(address=f"0x{i}", symbol=f"S{i}", volume=400000 + i,
                                     price_change_percent=1) for i in range(9)])
        conf = cfg(hot=hot_cfg(near_limit=3))
        near = [l for l in hot.digest_text(self.conn, conf, T0 + 60).split("\n")
                if l.startswith("- S")]
        self.assertEqual(len(near), 3)

    def test_near_tie_is_sorted_by_volume(self):
        """近さ（しきい値への達成度）が同じなら出来高の大きい順。"""
        hot.save_obs(self.conn, [
            obs(address="0x1", symbol="SMALL", volume=310000, price_change_percent=1),
            obs(address="0x2", symbol="BIG", volume=990000, price_change_percent=1)])
        near = [l for l in hot.digest_text(self.conn, cfg(), T0 + 60).split("\n")
                if l.startswith("- ")]
        self.assertTrue(near[0].startswith("- BIG"))

    def test_near_is_sorted_by_closeness(self):
        """惜しいは出来高順ではなく近い順。勢い不足は 24h ÷ burst.chg24_pct、規模不足は各項目の達成度の最小。

        出来高順だと 24h が横ばいの大型（QQQB・ZEC 等）が上位を埋めていた。
        """
        hot.save_obs(self.conn, [
            obs(address="0x1", symbol="FLAT", volume=990000, price_change_percent=1),        # 0.01
            obs(address="0x2", symbol="ALMOST", volume=310000, price_change_percent=90),     # 0.9
            obs(address="0x3", symbol="SHORT", volume=1_500_000, holder_count=280),          # 280/300
            obs(address="0x4", symbol="FAR", volume=350000, holder_count=100)])              # 100/300
        near = [l.split(" ")[1] for l in hot.digest_text(self.conn, cfg(), T0 + 60).split("\n")
                if l.startswith("- ")]
        self.assertEqual(near, ["SHORT", "ALMOST", "FAR", "FLAT"])

    def test_near_counts_swaps_by_the_alternative_route(self):
        """規模不足の取引の達成度は judge の代替ルートと揃える（保有者 alt_holders 以上なら alt_swaps に対する達成度も見る）。

        取引 1,600 は保有者 1,200 なら代替で足りている（judge は出来高だけ不足）。size.swaps 3,000 に対する 53% で数えると、
        出来高だけ 88% の ALT が全項目 67% の EVEN より下に並んでいた。保有者が alt_holders 未満なら size.swaps で測る。
        """
        hot.save_obs(self.conn, [
            obs(address="0x1", symbol="EVEN", volume=200000, swaps=2000, holder_count=200),   # 全部 2/3
            obs(address="0x2", symbol="ALT", volume=264000, swaps=1600, holder_count=1200,
                market_cap=250000),    # 出来高 0.88 だけ(ピークは floor 超え・alt_peak_mcap_usd 未満で出来高の代替は不成立)
            obs(address="0x3", symbol="FEW", volume=264000, swaps=1600, holder_count=900)])    # 取引 1600/3000
        text = hot.digest_text(self.conn, cfg(), T0 + 60)
        near = [l.split(" ")[1] for l in text.split("\n") if l.startswith("- ")]
        self.assertEqual(near, ["ALT", "EVEN", "FEW"])
        self.assertIn("- ALT 出来高 $264K / 取引 1,600 / 保有者 1,200 / 24h +480% → 規模不足（出来高 $264K）", text)

    def test_other_days_are_not_counted(self):
        hot.save_obs(self.conn, [obs(ts=T0 - 86400)])
        self.conn.execute("INSERT INTO hot_hits(chain,address,day_jst,symbol,confirmed_ts)"
                          " VALUES('bsc','0xaaa111',?,'MEME',?)",
                          (hot.jst_day(T0 - 86400), T0 - 86400))
        self.assertIn("確定 0 件", hot.digest_text(self.conn, cfg(), T0))


class TestDigestWindow(HotCase):
    """まとめの確定一覧は「前回まとめに載せた最大の確定 id より後」（day_jst でも時刻でも切らない）。"""

    D23 = hot.jst_day_start(T0) + 23 * 3600    # 09-19 23:00 JST（今日のまとめ）
    PREV = D23 - 86400                         # 09-18 23:00 JST（前回のまとめ）

    def add_hit(self, ts, symbol, chain="bsc"):
        address = "0x" + symbol.lower()
        self.conn.execute(
            "INSERT INTO hot_hits(chain,address,day_jst,symbol,confirmed_ts,notified_ts,sent,"
            "row_json,wallets_json) VALUES(?,?,?,?,?,?,1,?,'[]')",
            (chain, address, hot.jst_day(ts), symbol, ts, ts,
             json.dumps(obs(address=address, symbol=symbol, chain=chain))))

    def hit_id(self, symbol):
        return self.conn.execute(
            "SELECT id FROM hot_hits WHERE symbol=?", (symbol,)).fetchone()["id"]

    def listed(self, text):
        return [l.split(" ")[1] for l in text.split("\n") if l[:1].isdigit()]

    def digest(self, now):
        """cmd_digest と同じく digest_hits で拾った確定を渡したまとめ本文（hits なしは /hot になる）。"""
        hits, _ = hot.digest_hits(self.conn, now)
        return hot.digest_text(self.conn, cfg(), now, hits)

    def test_late_confirmation_is_in_the_next_digest(self):
        """前日 23:30 の確定（day_jst は前日）が今日 23:00 のまとめに載る。

        digest_text を hits なしで呼ぶと /hot（今日の確定）になったので、まとめの経路（digest_hits）で拾う。
        """
        self.add_hit(self.PREV - 600, "OLD")
        self.add_hit(self.PREV, "EDGE")            # 前回のまとめ時刻ちょうど＝前回に載った
        edge_id = self.hit_id("EDGE")
        db.kv_set(self.conn, "hot.last_digest_ts", self.PREV)
        db.kv_set(self.conn, "hot.last_digest_id", edge_id)
        self.add_hit(self.PREV - 30, "RACE")        # 前回まとめの前に確定したが、書き込みは後
        self.add_hit(self.PREV + 1800, "LATE")
        self.add_hit(self.D23, "NOW")
        text = self.digest(self.D23)
        self.assertEqual(text.split("\n")[0],
                         f"【今日の盛り上がり】{DAY}（BSC）確定 3 件（09-18 23:00 以降）")
        self.assertEqual(self.listed(text), ["RACE", "LATE", "NOW"])

    def test_without_a_valid_kv_it_is_24_hours(self):
        """kv が無効なら直近 24 時間。上限（confirmed_ts <= now）は外したので now より後の NEXT も載る。

        読んだ時点でコミット済みの行を載せないと、起点（MAX(id)）がその行を越えて永久に載らなくなるため。
        """
        now = self.D23
        self.add_hit(now - 86400 + 60, "IN")
        self.add_hit(now - 86400, "OUT")
        self.add_hit(now + 1, "NEXT")
        for raw in (None, "abc", "nan", str(now), str(now + 3600)):
            if raw is not None:
                db.kv_set(self.conn, "hot.last_digest_ts", raw)
            text = self.digest(now)
            self.assertTrue(text.split("\n")[0].endswith("確定 2 件（09-18 23:00 以降）"), raw)
            self.assertEqual(self.listed(text), ["IN", "NEXT"], raw)

        # hot.last_digest_id が無効なら、hot.last_digest_ts の値によらず直近 24 時間（初回扱い）
        self.conn.execute("DELETE FROM kv WHERE key=?", ("hot.last_digest_ts",))
        for raw in (None, "abc", "nan", "inf"):
            if raw is not None:
                db.kv_set(self.conn, "hot.last_digest_id", raw)
            text = self.digest(now)
            self.assertEqual(self.listed(text), ["IN", "NEXT"], raw)
            self.assertTrue(text.split("\n")[0].endswith("確定 2 件（09-18 23:00 以降）"), raw)

    def test_fallback_keeps_rows_after_now_and_saves_the_top_id(self):
        """kv が無いときの 24 時間は now より後の行も載せ、確定 0 件でも起点は MAX(id) を返す。"""
        self.assertEqual(hot.digest_hits(self.conn, self.D23), ([], None))   # 表が空なら起点なし
        self.add_hit(self.D23 - 2 * 86400, "OLD")     # 24 時間より前
        hits, last_id = hot.digest_hits(self.conn, self.D23)
        self.assertEqual((hits, last_id), ([], self.hit_id("OLD")))
        self.add_hit(self.D23 + 1, "NEXT")            # まとめの時刻より後に確定（読んだ時点でコミット済み）
        hits, last_id = hot.digest_hits(self.conn, self.D23)
        self.assertEqual([h["symbol"] for h in hits], ["NEXT"])
        self.assertEqual(last_id, self.hit_id("NEXT"))

    def test_cursor_beyond_the_table_falls_back_to_24_hours(self):
        """kv の起点が MAX(id) より大きい（hot_hits を作り直した等）なら警告を出して直近 24 時間で拾う。"""
        self.add_hit(self.D23 - 600, "A")
        self.add_hit(self.D23 - 2 * 86400, "OLD")
        db.kv_set(self.conn, "hot.last_digest_id", 999)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            hits, last_id = hot.digest_hits(self.conn, self.D23)
        self.assertEqual([h["symbol"] for h in hits], ["A"])
        self.assertEqual(last_id, self.hit_id("OLD"))    # MAX(id)
        self.assertIn("hot.last_digest_id=999", err.getvalue())
        # 起点が MAX(id) ちょうどなら id で切る（警告なし）
        db.kv_set(self.conn, "hot.last_digest_id", self.hit_id("OLD"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(hot.digest_hits(self.conn, self.D23), ([], self.hit_id("OLD")))
        self.assertEqual(err.getvalue(), "")

    def test_other_chains_are_listed_with_a_mark(self):
        """/chains で外したチェーンの確定も絞らずに載せ、[ARC] の印を付ける（絞るとカーソルだけ越えて消える）。"""
        self.add_hit(self.D23 - 1200, "ARCY", chain="arc")
        self.add_hit(self.D23 - 600, "BSCY")
        self.conn.commit()
        send, _out = self.run_digest(self.D23)
        lines = [l for l in send.call_args[0][0].split("\n") if l[:1].isdigit()]
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("1. [ARC] ARCY 出来高 $1.5M"), lines[0])
        self.assertTrue(lines[1].startswith("2. BSCY 出来高 $1.5M"), lines[1])
        self.assertIn("確定 2 件", send.call_args[0][0].split("\n")[0])
        self.assertEqual(db.kv_get(self.conn, "hot.last_digest_id"), str(self.hit_id("BSCY")))

    def test_open_row_waits_for_the_next_digest(self):
        """処理中の行（notified_ts が NULL で巡回が未完了）は載せず、起点をその手前（cut - 1）で止める。

        close_hit して巡回が終わった後の次のまとめに、中身付きで載る。
        """
        self.add_hit(self.D23 - 1200, "A")
        db.kv_set(self.conn, "hot.last_cycle_ts", self.D23 - 1200)
        b = obs(address="0xbbb", symbol="B")
        self.assertTrue(hot.open_hit(self.conn, b, self.D23 - 60))   # GMGN を取りに行っている最中
        self.add_hit(self.D23 - 30, "C")
        self.conn.commit()
        send, _out = self.run_digest(self.D23)
        self.assertEqual(self.listed(send.call_args[0][0]), ["A"])
        self.assertEqual(db.kv_get(self.conn, "hot.last_digest_id"), str(self.hit_id("B") - 1))

        hot.close_hit(self.conn, b, self.D23 - 60, "本文", [], True)
        db.kv_set(self.conn, "hot.last_cycle_ts", self.D23 - 60)    # その巡回が最後まで回った
        self.conn.commit()
        send, _out = self.run_digest(self.D23 + 86400)
        text = send.call_args[0][0]
        self.assertEqual(self.listed(text), ["B", "C"])
        self.assertIn("1. B 出来高 $1.5M / 取引 12,340 / 保有者 2,310 / 24h +480%"
                      " / 有名 0 / 通知 22:59", text)
        self.assertEqual(db.kv_get(self.conn, "hot.last_digest_id"), str(self.hit_id("C")))

    def test_crashed_row_is_listed_as_unfinished(self):
        """巡回が open_hit と close_hit の間で落ちた残骸（後の巡回は終わっている）は「未完了」で載せて越える。"""
        self.assertTrue(hot.open_hit(self.conn, obs(address="0xdead", symbol="DEAD"),
                                     self.D23 - 1800))
        db.kv_set(self.conn, "hot.last_cycle_ts", self.D23 - 600)
        self.conn.commit()
        send, _out = self.run_digest(self.D23)
        text = send.call_args[0][0]
        self.assertEqual(self.listed(text), ["DEAD"])
        self.assertIn("/ 有名 0 / 通知 未完了", text)
        self.assertEqual(db.kv_get(self.conn, "hot.last_digest_id"), str(self.hit_id("DEAD")))

    def test_hot_command_shows_rows_without_a_body_from_hot_obs(self):
        """/hot は本文の無い行を同じ巡回の観測（hot_obs の ts=confirmed_ts）の数値で出し、処理中と未完了を分ける。

        巡回は open_hit の前に save_obs で同じ時刻の観測を保存している。処理中（作った巡回が GMGN を取っている最中）は
        /hot でだけ見え、まとめは次回に回す。観測が無ければ今までどおり 0 のまま出す。
        """
        dead = obs(ts=self.D23 - 1800, address="0xdead", symbol="DEAD", volume=420000,
                   swaps=4100, holder_count=610, price_change_percent=250)
        busy = obs(ts=self.D23 - 60, address="0xbusy", symbol="BUSY", volume=880000,
                   swaps=5200, holder_count=930, price_change_percent=150)
        hot.save_obs(self.conn, [dead, busy])
        self.assertTrue(hot.open_hit(self.conn, dead, self.D23 - 1800))   # この巡回は途中で落ちた
        db.kv_set(self.conn, "hot.last_cycle_ts", self.D23 - 600)         # 後の巡回は最後まで回った
        self.assertTrue(hot.open_hit(self.conn, busy, self.D23 - 60))     # 今の巡回が GMGN を取っている最中
        self.assertTrue(hot.open_hit(self.conn, obs(address="0xnone", symbol="NONE"), self.D23 - 60))
        self.conn.commit()
        text = hot.digest_text(self.conn, cfg(), self.D23)
        self.assertEqual([l for l in text.split("\n") if l[:1].isdigit()], [
            "1. DEAD 出来高 $420K / 取引 4,100 / 保有者 610 / 24h +250% / 有名 0 / 通知 未完了",
            "2. BUSY 出来高 $880K / 取引 5,200 / 保有者 930 / 24h +150% / 有名 0 / 通知 処理中",
            "3. NONE 出来高 ? / 取引 0 / 保有者 0 / 24h +0% / 有名 0 / 通知 処理中"])
        # まとめ（digest_hits 経由）は処理中の 2 行を次回に回し、残骸だけを観測の数値付きの「未完了」で載せる
        text = self.digest(self.D23)
        self.assertEqual(self.listed(text), ["DEAD"])
        self.assertIn("1. DEAD 出来高 $420K / 取引 4,100 / 保有者 610 / 24h +250% / 有名 0 / 通知 未完了", text)

    def test_digest_during_a_real_cycle_waits_for_the_open_row(self):
        """本物の cycle() の送信中（open_hit と close_hit の間）に読むと、確定行は処理中で次のまとめに回る。

        巡回は hot.last_cycle_ts を close_hit の後（最後）に書く。最初に書くと送信中の行が「終わった巡回の残骸」に見え、
        中身が空のまま載って起点が越え、二度と中身付きで載らない。/hot はこの間「処理中」と観測の数値で出す。
        """
        fake = FakeGMGN(trending={"swaps": [trend()]})
        self.run_cycle(fake)                                          # 1 周目（streak 1）
        seen = {}

        def sender(text):
            seen["digest"] = hot.digest_hits(self.conn, T0 + 630)
            seen["hot"] = hot.digest_text(self.conn, cfg(), T0 + 630)
            return True
        self.run_cycle(fake, now=T0 + 600, sender=sender)             # 2 周目で確定し、その送信中に読む
        hit_id = self.hit_id("MEME")
        self.assertEqual(seen["digest"], ([], hit_id - 1))
        self.assertIn("1. MEME 出来高 $1.5M / 取引 12,340 / 保有者 2,310 / 24h +480% / 有名 0 / 通知 処理中",
                      seen["hot"])
        hits, last_id = hot.digest_hits(self.conn, T0 + 700)           # 巡回が終わった後のまとめ
        self.assertEqual(([h["symbol"] for h in hits], last_id), (["MEME"], hit_id))
        self.assertEqual(hits[0]["notified_ts"], T0 + 600)
        self.assertEqual(json.loads(hits[0]["row_json"])["symbol"], "MEME")

    def test_cycle_finishing_while_the_kv_is_read_is_listed_with_its_body(self):
        """hot.last_cycle_ts を読む処理の中で巡回が終わっても、行はその後に読むので中身付きで載り、起点はその id。

        行を先に読むと（読む順を入れ替えると）、読んだ時点で中身の無い行を「巡回は終わった＝残骸」と取り違えて
        「未完了」で載せ、起点が越えて二度と中身付きで載らない。
        """
        self.add_hit(self.D23 - 1200, "A")
        db.kv_set(self.conn, "hot.last_cycle_ts", self.D23 - 1200)
        b = obs(address="0xbbb", symbol="B")
        self.assertTrue(hot.open_hit(self.conn, b, self.D23 - 60))    # GMGN を取りに行っている最中
        self.conn.commit()
        real_kv_get = db.kv_get

        def kv_get(conn, key, default=None):
            if key == "hot.last_cycle_ts":
                # 読まれた瞬間にその巡回が最後まで回る（close_hit の後に hot.last_cycle_ts を書く）
                hot.close_hit(conn, b, self.D23 - 60, "本文", [], True)
                db.kv_set(conn, "hot.last_cycle_ts", self.D23 - 60)
            return real_kv_get(conn, key, default)
        with mock.patch.object(hot.db, "kv_get", kv_get):
            hits, last_id = hot.digest_hits(self.conn, self.D23)
        self.assertEqual([(h["symbol"], h["notified_ts"]) for h in hits],
                         [("A", self.D23 - 1200), ("B", self.D23 - 60)])
        self.assertEqual(last_id, self.hit_id("B"))
        self.assertIn("2. B 出来高 $1.5M / 取引 12,340 / 保有者 2,310 / 24h +480% / 有名 0 / 通知 22:59",
                      hot.digest_text(self.conn, cfg(), self.D23, hits))

    def test_hit_written_while_sending_is_in_the_next_digest(self):
        """送信（notify.send）の最中に別の巡回が書いた確定は、今回の起点に含めず次のまとめに載せる。"""
        self.add_hit(self.D23 - 600, "A")
        self.conn.commit()

        def race():
            self.add_hit(self.D23 + 5, "MID")
            self.conn.commit()
        send, _out = self.run_digest(self.D23, during=race)
        self.assertEqual(self.listed(send.call_args[0][0]), ["A"])
        self.assertEqual(db.kv_get(self.conn, "hot.last_digest_id"), str(self.hit_id("A")))
        send, _out = self.run_digest(self.D23 + 86400)
        self.assertEqual(self.listed(send.call_args[0][0]), ["MID"])

    def test_hot_command_lists_today_after_the_digest(self):
        """/hot（hits なし）は今日（JST）の確定を全部出す。23:00 のまとめで起点が進んだ後も消えず、
        前日 23 時台の確定は出さない。見出しに「以降」は付けず、kv も書かない。"""
        self.add_hit(self.PREV + 1800, "LATE")      # 前日 23:30（day_jst は前日）
        self.add_hit(self.D23 - 3600, "EARLY")
        self.add_hit(self.D23 - 600, "LAST")
        self.conn.commit()
        self.run_digest(self.D23)                  # 起点が LAST まで進む
        self.add_hit(self.D23 + 1800, "NIGHT")     # まとめの後（今日 23:30）
        self.conn.commit()
        kv = [db.kv_get(self.conn, k) for k in ("hot.last_digest_ts", "hot.last_digest_id")]
        before = self.conn.total_changes
        text = hot.digest_text(self.conn, cfg(), self.D23 + 1900)
        self.assertEqual(text.split("\n")[0], f"【今日の盛り上がり】{DAY}（BSC）確定 3 件")
        self.assertEqual(self.listed(text), ["EARLY", "LAST", "NIGHT"])
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual([db.kv_get(self.conn, k) for k in ("hot.last_digest_ts", "hot.last_digest_id")],
                         kv)

    def test_digest_text_does_not_write(self):
        """/hot からも呼ばれるので digest_text は kv を進めない（何も書かない）。"""
        import watch
        self.add_hit(self.PREV + 1800, "LATE")
        self.conn.commit()
        before = self.conn.total_changes
        hot.digest_text(self.conn, cfg(), self.D23)
        watch.dispatch_command(self.conn, cfg(), "/hot")
        self.assertEqual(self.conn.total_changes, before)
        self.assertIsNone(db.kv_get(self.conn, "hot.last_digest_ts"))
        self.assertIsNone(db.kv_get(self.conn, "hot.last_digest_id"))

    def run_digest(self, now, sent=True, creds=("tok", "chat"), during=None):
        """cmd_digest を一時 DB・固定の設定・送信モックで 1 回。GMGN も Telegram も呼ばない。

        during は送信の最中に呼ぶ関数（別の巡回が確定を書き込む場面を作る）。
        """
        real_connect, path = db.connect, os.path.join(self.dir, "state.db")
        out = io.StringIO()

        def fake_send(text, token, chat_id):
            if during:
                during()
            return {"message_id": 1} if sent else None
        with mock.patch.object(hot.db, "connect", lambda *a, **k: real_connect(path)), \
                mock.patch.object(hot, "load_config", return_value=cfg()), \
                mock.patch.object(hot.notify, "credentials", return_value=creds), \
                mock.patch.object(hot.notify, "send", side_effect=fake_send) as send, \
                mock.patch.object(hot.time, "time", return_value=now), \
                contextlib.redirect_stdout(out):
            hot.cmd_digest(None)
        return send, out.getvalue()

    NOT_SENT = "hot digest NOT sent（起点は進めない。次のまとめに同じ確定をもう一度載せる）"

    def test_cmd_digest_advances_only_after_a_successful_send(self):
        self.add_hit(self.D23 - 600, "A")
        self.conn.commit()
        a_id = self.conn.execute(
            "SELECT id FROM hot_hits WHERE symbol=?", ("A",)).fetchone()["id"]
        send, out = self.run_digest(self.D23, sent=False)
        send.assert_called_once()
        self.assertIsNone(db.kv_get(self.conn, "hot.last_digest_ts"))
        self.assertIsNone(db.kv_get(self.conn, "hot.last_digest_id"))
        # 送れなかったときは本文の前に 1 行（launchd のログで目立たせる）
        self.assertEqual(out.split("\n")[0], "[2026-09-19 23:00] " + self.NOT_SENT)
        self.assertTrue(out.split("\n")[1].startswith("【今日の盛り上がり】"))
        send, out = self.run_digest(self.D23, creds=(None, None))
        send.assert_not_called()
        self.assertIsNone(db.kv_get(self.conn, "hot.last_digest_ts"))
        self.assertIsNone(db.kv_get(self.conn, "hot.last_digest_id"))
        self.assertEqual(out.split("\n")[0], "[2026-09-19 23:00] " + self.NOT_SENT)
        send, out = self.run_digest(self.D23)
        send.assert_called_once()
        self.assertTrue(send.call_args[0][0].startswith("【今日の盛り上がり】"))
        self.assertIn("hot digest sent", out)
        self.assertEqual(db.kv_get(self.conn, "hot.last_digest_ts"), str(self.D23))
        self.assertEqual(db.kv_get(self.conn, "hot.last_digest_id"), str(a_id))

    def test_next_digest_starts_from_the_last_sent_one(self):
        """送れたまとめの時刻が次の起点。その後の 23:30 の確定は翌日のまとめに載る。"""
        self.add_hit(self.D23 - 600, "EARLY")
        self.conn.commit()
        self.run_digest(self.D23)
        self.add_hit(self.D23 - 30, "RACE")        # 1 回目のまとめより前に確定、書き込みは後
        self.add_hit(self.D23 + 1800, "LATE")
        self.conn.commit()
        late_id = self.conn.execute(
            "SELECT id FROM hot_hits WHERE symbol=?", ("LATE",)).fetchone()["id"]
        send, _out = self.run_digest(self.D23 + 86400)
        text = send.call_args[0][0]
        self.assertTrue(text.split("\n")[0].endswith("確定 2 件（09-19 23:00 以降）"))
        self.assertEqual(self.listed(text), ["RACE", "LATE"])
        self.assertEqual(db.kv_get(self.conn, "hot.last_digest_ts"), str(self.D23 + 86400))
        self.assertEqual(db.kv_get(self.conn, "hot.last_digest_id"), str(late_id))


# --- そのほか ----------------------------------------------------------------

class TestLaneLine(HotCase):
    def test_never_ran(self):
        self.assertEqual(hot.lane_line(self.conn, T0),
                         "盛り上がりレーン: 最終巡回 なし・今日の確定 0 件")

    def test_after_a_cycle(self):
        self.run_cycle(FakeGMGN(trending={"swaps": [trend()]}))
        self.assertEqual(hot.lane_line(self.conn, T0 + 300),
                         "盛り上がりレーン: 最終巡回 15:40（5 分前）・今日の確定 0 件")


class TestJstDay(unittest.TestCase):
    def test_boundaries(self):
        midnight = hot.jst_day_start(T0)
        self.assertEqual(hot.fmt_jst(midnight, "%Y-%m-%d %H:%M"), f"{DAY} 00:00")
        self.assertEqual(hot.jst_day(midnight), DAY)
        self.assertEqual(hot.jst_day(midnight - 1), hot.jst_day(T0 - 86400))
        self.assertEqual(hot.jst_day_start(midnight + 86399), midnight)


class TestPrune(HotCase):
    def test_old_observations_go_away(self):
        hot.save_obs(self.conn, [obs(ts=T0 - 20 * 86400), obs(ts=T0, address="0xB")])
        hot.prune(self.conn, T0, 14)
        left = [r["ts"] for r in self.conn.execute("SELECT ts FROM hot_obs")]
        self.assertEqual(left, [T0])


class TestConfigBlock(unittest.TestCase):
    def test_defaults_come_from_watch(self):
        import watch
        self.assertIs(hot.DEFAULTS, watch.DEFAULTS["hot"])
        for key in ("size", "burst", "confirm_passes", "cooldown_hours", "pulls",
                    "wallet_tags", "wallet_exclude_tags", "near_limit", "retention_days",
                    "interval_sec", "api_gap_sec"):
            self.assertIn(key, hot.DEFAULTS)

    def test_every_threshold_is_settable(self):
        import watch
        c = {"hot": json.loads(json.dumps(hot.DEFAULTS))}
        ok, _msg, updates = watch.set_config_value(c, "hot.size.swaps", "5000")
        self.assertTrue(ok)
        self.assertEqual(updates["hot"]["size"]["swaps"], 5000)

    NEW_KEYS = (("size", "alt_holders"), ("size", "alt_swaps"), ("burst", "day_range_chg24_pct"),
                ("burst", "holder_growth_pct"), ("burst", "holder_chg24_pct"),
                ("size", "peak_mcap_usd"), ("size", "alt_volume_usd"), ("size", "alt_peak_mcap_usd"))

    def test_new_keys_are_settable(self):
        import watch
        for part, key in self.NEW_KEYS:
            c = {"hot": json.loads(json.dumps(hot.DEFAULTS))}
            ok, _msg, updates = watch.set_config_value(c, f"hot.{part}.{key}", "7")
            self.assertTrue(ok, key)
            self.assertEqual(updates["hot"][part][key], 7)
        # bot の /set と同じ経路（dispatch_command → set_config_value → save_config）
        c = {"hot": json.loads(json.dumps(hot.DEFAULTS))}
        c["hot"]["size"]["alt_swaps"] = 2000
        with mock.patch.object(watch, "save_config") as save:
            reply = watch.dispatch_command(None, c, "/set hot.size.alt_swaps 1500")
        self.assertEqual(reply, "hot.size.alt_swaps: 2000 → 1500")
        save.assert_called_once()
        self.assertEqual(save.call_args[0][0]["hot"]["size"]["alt_swaps"], 1500)

    def test_load_config_fills_missing_nested_keys(self):
        """config.json の size / burst に新キーが無くても既定で埋め、ファイルにも書き足す（値は上書きしない）。"""
        import watch
        path = os.path.join(tempfile.mkdtemp(prefix="hot-config-"), "config.json")
        old = json.loads(json.dumps(hot.DEFAULTS))
        for part, key in self.NEW_KEYS:
            del old[part][key]
        old["size"]["swaps"] = 5000
        old["burst"]["holder_growth"] = 400
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"chains": ["bsc", "arc"], "dry_run": False, "hot": old}, f)
        size = {**hot.DEFAULTS["size"], "swaps": 5000}
        burst = {**hot.DEFAULTS["burst"], "holder_growth": 400}
        with mock.patch.object(watch, "CONFIG_PATH", path):
            c = hot.load_config()
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            with mock.patch.object(watch, "save_config") as save:
                again = hot.load_config()
        self.assertEqual((c["hot"]["size"], c["hot"]["burst"]), (size, burst))
        self.assertEqual((saved["hot"]["size"], saved["hot"]["burst"]), (size, burst))
        self.assertEqual((saved["chains"], saved["dry_run"]), (["bsc", "arc"], False))
        save.assert_not_called()                  # 埋まった後は書き直さない
        self.assertEqual(again["hot"], c["hot"])
        self.assertEqual(hot.DEFAULTS["size"]["swaps"], 3000)   # 既定そのものは書き換えない

    def test_load_config_accepts_null_parts(self):
        """config.json に "size": null / "burst": null と書かれていても落ちず、既定で埋めて書き足す。"""
        import watch
        path = os.path.join(tempfile.mkdtemp(prefix="hot-config-"), "config.json")
        stored = json.loads(json.dumps(hot.DEFAULTS))
        stored["size"] = None
        stored["burst"] = None
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"chains": ["bsc"], "hot": stored}, f)
        with mock.patch.object(watch, "CONFIG_PATH", path):
            c = hot.load_config()
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
        self.assertEqual((c["hot"]["size"], c["hot"]["burst"]),
                         (hot.DEFAULTS["size"], hot.DEFAULTS["burst"]))
        self.assertEqual((saved["hot"]["size"], saved["hot"]["burst"]),
                         (hot.DEFAULTS["size"], hot.DEFAULTS["burst"]))


if __name__ == "__main__":
    unittest.main()
