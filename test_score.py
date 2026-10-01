"""EMS（Early Momentum Score）のテスト（§12.8 の受け入れ条件）。

採点は純粋関数なので合成データで固定する。平常値（baseline）と通知の抑制は
:memory: の SQLite を相手に確かめる。ネットワークにも launchd にも触らない。
"""
import json
import math
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import baseline    # noqa: E402
import db          # noqa: E402
import rules       # noqa: E402
import score_log   # noqa: E402
import watch       # noqa: E402
from chain import agg as agg_mod, run as chain_run, store  # noqa: E402

NOW = 1_000_000


def _cfg(**over):
    cfg = json.loads(json.dumps(watch.DEFAULTS))
    cfg["dry_run"] = False
    cfg["score"].update(over)
    return cfg


def _feat(**kw):
    """ゲートを通り最低証拠も満たす「何も起きていない」特徴量。"""
    f = {"wins": [10, 30, 120], "flow_w": 30, "address": "0xtok", "symbol": "SYM",
         "launchpad": None, "age_min": 600.0, "price": 1.0, "liq": 50000.0,
         "U10": 0, "B10": 0, "Vb10": 0.0, "Vs10": 0.0, "Net10": 0.0,
         "U30": 3, "B30": 3, "Vb30": 0.0, "Vs30": 0.0, "Net30": 0.0,
         "U120": 5, "B120": 5, "Vb120": 0.0, "Vs120": 0.0, "Net120": 0.0,
         "bsi": 0.0, "U300": 5, "B300": 5, "Top1_300": 0.0, "roundtrip_300": 0.0,
         "pc120": None, "mu_rate": 1.0, "med": math.log1p(100), "mad": 0.5,
         "cohort": False, "z_market": 0.0, "n_listed_10m": 0, "bundle": False,
         "g30_prev": None, "raised_usd": None}
    f.update(kw)
    return f


def _ctx(**kw):
    c = {"liquidity": 50000.0, "mcap": 500000.0, "wash": False, "honeypot": False,
         "grad_usd": None, "snapshots": [], "holders": None, "smart": None,
         "visiting": None, "now": NOW}
    c.update(kw)
    return c


class Trade:
    """chain/decode.py の Trade の最小版（集計が読む属性だけ）。"""

    def __init__(self, ts, side, usd, trader, price=1.0, address="0xtok", **kw):
        self.ts, self.side, self.usd, self.trader, self.price = ts, side, usd, trader, price
        self.address, self.block, self.dex = address, kw.get("block", 1), "pancake-v2"
        self.liq, self.supply = kw.get("liq"), kw.get("supply")
        self.symbol, self.launchpad = kw.get("symbol"), kw.get("launchpad")
        self.created_ts = kw.get("created_ts")


# --- 驚きの大きさ（G と z）---------------------------------------------------

class TestSurprise(unittest.TestCase):
    def test_poisson_is_zero_until_it_beats_the_normal(self):
        self.assertEqual(rules.poisson_g(1, 2), 0.0)
        self.assertEqual(rules.poisson_g(2, 2), 0.0)
        self.assertGreater(rules.poisson_g(3, 2), 0.0)
        self.assertGreater(rules.poisson_g(6, 0.5), rules.poisson_g(5, 0.5))

    def test_buyers_reach_full_marks_at_4_3(self):
        # 平常 1 人/分の銘柄に 30 秒で 6 人（μ=0.5）＝ G 4.34
        res = rules.score(_feat(U30=6), _cfg(), _ctx())
        self.assertAlmostEqual(rules.poisson_g(6, 0.5), 4.338, places=3)
        self.assertEqual(res["parts"]["buyers"], 1.0)
        self.assertEqual(round(res["points"]["buyers"]), 25)
        self.assertLess(rules.score(_feat(U30=5), _cfg(), _ctx())["parts"]["buyers"], 1.0)

    def test_volume_uses_the_minute_rate_against_the_median(self):
        # Vb30=$400 → 分レート $800。log1p(800)=6.685、med=log1p(100)、mad=0.5 で z=4.1
        res = rules.score(_feat(Vb30=400.0), _cfg(), _ctx())
        z = (math.log1p(800) - math.log1p(100)) / 0.5
        self.assertAlmostEqual(z, 4.141, places=3)
        self.assertEqual(res["parts"]["vol"], 1.0)
        half = rules.score(_feat(Vb30=200.0), _cfg(), _ctx())
        self.assertAlmostEqual(half["parts"]["vol"],
                               (math.log1p(400) - math.log1p(100)) / 0.5 / 4, places=6)

    def test_market_surge_is_subtracted_from_both(self):
        """チェーン全体が沸いている分（κ·z_market）は G からも z からも引く。"""
        quiet = rules.score(_feat(U30=6, Vb30=400.0), _cfg(), _ctx())
        busy = rules.score(_feat(U30=6, Vb30=400.0, z_market=4.0), _cfg(), _ctx())
        self.assertEqual(quiet["parts"]["buyers"], 1.0)
        self.assertAlmostEqual(busy["parts"]["buyers"],
                               (rules.poisson_g(6, 0.5) - 2.0) / 4.3, places=6)
        self.assertLess(busy["parts"]["vol"], quiet["parts"]["vol"])

    def test_gain_moves_the_full_mark_line(self):
        loose = rules.score(_feat(U30=6), _cfg(gain=0.5), _ctx())["parts"]["buyers"]
        tight = rules.score(_feat(U30=6), _cfg(gain=2.0), _ctx())["parts"]["buyers"]
        self.assertEqual(loose, 1.0)
        self.assertLess(tight, 0.6)


class TestEvidence(unittest.TestCase):
    """買い手が少なすぎる間は、驚きが大きく出ても買い手と加速では点を出さない。"""

    def test_thin_evidence_kills_buyers_and_accel(self):
        thin = rules.score(_feat(U30=2, U120=4, g30_prev=0.0), _cfg(), _ctx())
        self.assertEqual(thin["parts"]["buyers"], 0.0)
        self.assertEqual(thin["parts"]["accel"], 0.0)

    def test_three_buyers_in_30s_is_enough(self):
        ok = rules.score(_feat(U30=3, U120=4), _cfg(), _ctx())
        self.assertGreater(ok["parts"]["buyers"], 0.0)

    def test_accel_is_the_difference_of_g(self):
        f = _feat(U30=6, g30_prev=rules.poisson_g(6, 0.5) - 1.0)
        self.assertAlmostEqual(rules.score(f, _cfg(), _ctx())["parts"]["accel"], 0.5)


# --- ゲート・除外 ------------------------------------------------------------

class TestGate(unittest.TestCase):
    def test_thin_liquidity_is_not_scored(self):
        res = rules.score(_feat(), _cfg(), _ctx(liquidity=19_999))
        self.assertEqual(res["reason"], "gate")
        self.assertEqual(res["score"], 0.0)

    def test_new_tokens_get_the_lower_bar(self):
        res = rules.score(_feat(age_min=30.0, U30=6), _cfg(), _ctx(liquidity=10_000))
        self.assertEqual(res["reason"], "")
        self.assertGreater(res["score"], 0)

    def test_fourmeme_before_graduation_uses_buyers_instead_of_liquidity(self):
        f = _feat(launchpad="four.meme", raised_usd=1000.0, U120=5, liq=None)
        res = rules.score(f, _cfg(), _ctx(liquidity=None, grad_usd=6000.0))
        self.assertEqual(res["reason"], "")
        self.assertTrue(res["pre_grad"])
        self.assertAlmostEqual(res["progress"], 1000.0 / 6000.0)

    def test_bundle_closes_the_gate_before_graduation(self):
        f = _feat(launchpad="four.meme", raised_usd=1000.0, U120=5, liq=None, bundle=True)
        res = rules.score(f, _cfg(), _ctx(liquidity=None, grad_usd=6000.0))
        self.assertEqual(res["reason"], "gate")

    def test_honeypot_is_excluded_outright(self):
        res = rules.score(_feat(U30=9, Vb30=9999.0), _cfg(), _ctx(honeypot=True))
        self.assertEqual(res["reason"], "honeypot")
        self.assertEqual(res["score"], 0.0)


class TestFlow(unittest.TestCase):
    def test_net_buy_against_liquidity(self):
        res = rules.score(_feat(Net30=0.02 * 50_000), _cfg(), _ctx())
        self.assertAlmostEqual(res["parts"]["flow"], 0.7)

    def test_before_graduation_the_curve_is_the_yardstick(self):
        f = _feat(launchpad="four.meme", raised_usd=1000.0, liq=None,
                  Net30=0.05 * 6000.0, U120=5)
        res = rules.score(f, _cfg(), _ctx(liquidity=None, grad_usd=6000.0))
        self.assertAlmostEqual(res["parts"]["flow"], 0.7)

    def test_without_a_yardstick_only_the_balance_counts(self):
        """純買いを測る物差し（流動性・卒業必要額）が無いときは買い越し比だけ見る。"""
        res = rules.score(_feat(Net30=None, bsi=1.0), _cfg(), _ctx())
        self.assertAlmostEqual(res["parts"]["flow"], 0.3)


class TestPenalties(unittest.TestCase):
    def _pen(self, **kw):
        return rules.score(_feat(**kw), _cfg(), _ctx(**kw.pop("ctx", {})))

    def test_same_wallet_churn(self):
        res = rules.score(_feat(B300=10, U300=2), _cfg(), _ctx())
        self.assertIn("same_wallet", res["penalties"])
        self.assertEqual(res["penalty"], 15)

    def test_one_wallet_dominates(self):
        res = rules.score(_feat(B300=3, Top1_300=0.6), _cfg(), _ctx())
        self.assertEqual(res["penalties"], ["top1"])

    def test_bundle_and_wash_and_roundtrip_and_dump(self):
        res = rules.score(_feat(bundle=True, roundtrip_300=0.6, Vs120=10.0, Vb120=5.0,
                                pc120=-3.0), _cfg(), _ctx(wash=True))
        self.assertEqual(sorted(res["penalties"]),
                         ["bundle", "dump", "roundtrip", "wash"])
        self.assertEqual(res["penalty"], 50)

    def test_penalty_cannot_push_the_score_below_zero(self):
        res = rules.score(_feat(bundle=True, roundtrip_300=0.6), _cfg(), _ctx(wash=True))
        self.assertEqual(res["score"], 0.0)


class TestWalletAndPrice(unittest.TestCase):
    def test_listed_wallets_and_history_add_up(self):
        snaps = [{"ts": NOW - 1800, "smart": 1, "holders": 100, "visiting": 100}]
        res = rules.score(_feat(n_listed_10m=1), _cfg(),
                          _ctx(snapshots=snaps, smart=4, holders=130, visiting=300))
        self.assertEqual(res["parts"]["wallet"], 1.0)
        self.assertEqual(res["wallet_hits"],
                         {"smart": True, "holders": True, "visiting": True})

    def test_price_needs_net_buying_and_is_damped_on_thin_books(self):
        res = rules.score(_feat(pc120=10.0, Net120=100.0), _cfg(), _ctx())
        self.assertEqual(res["parts"]["price"], 1.0)     # mcap/L = 10 → 重み 1
        thin = rules.score(_feat(pc120=10.0, Net120=100.0), _cfg(),
                           _ctx(mcap=50_000_000))       # mcap/L = 1000 → 重み 0.3
        self.assertAlmostEqual(thin["parts"]["price"], 0.3)
        self.assertEqual(rules.score(_feat(pc120=10.0, Net120=-1.0), _cfg(),
                                     _ctx())["parts"]["price"], 0.0)


class TestLevels(unittest.TestCase):
    def test_cuts(self):
        cuts = watch.DEFAULTS["score"]["levels"]
        self.assertEqual(rules.level_of(59.9, cuts), "")
        self.assertEqual(rules.level_of(60, cuts), "WATCH")
        self.assertEqual(rules.level_of(70, cuts), "HOT")
        self.assertEqual(rules.level_of(80, cuts), "STRONG")
        self.assertEqual(rules.level_of(99, cuts), "EXTREME")

    def test_full_marks_is_extreme(self):
        f = _feat(U30=9, Vb30=10_000.0, Net30=5000.0, bsi=1.0, pc120=20.0, Net120=10.0,
                  n_listed_10m=3, g30_prev=0.0)
        res = rules.score(f, _cfg(), _ctx())
        self.assertEqual(res["score"], 100.0)
        self.assertEqual(res["level"], "EXTREME")


# --- 平常値（全体フロアと同齢コホート）--------------------------------------

class TestBaseline(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        store.ensure(self.conn)
        self.minute = NOW // 60

    def _bucket(self, address, minute, buyers, vol):
        self.conn.execute(
            "INSERT OR REPLACE INTO chain_buckets"
            "(chain,address,minute,buys,sells,buyers,vol_usd) VALUES('bsc',?,?,?,0,?,?)",
            (address, minute, max(buyers, 1), buyers, vol))   # 取引のあった分だけ p50 に入る

    def test_hard_floor_wins_when_the_chain_is_quiet(self):
        for i in range(10):
            self._bucket("0xa", self.minute - i, 0, 1.0)
        data = baseline.compute(self.conn, "bsc", NOW)
        self.assertEqual(baseline.floors(data), (0.5, 100.0))
        self.assertEqual(data["buckets"], 10)

    def test_median_wins_when_the_chain_is_busy(self):
        for i in range(10):
            self._bucket("0xa", self.minute - i, 4, 5000.0)
        data = baseline.compute(self.conn, "bsc", NOW)
        self.assertEqual(baseline.floors(data), (4.0, 5000.0))

    def test_cohort_needs_five_samples_of_the_same_age(self):
        created = (self.minute - 10) * 60
        for i in range(6):     # 生後 0 分のセルを 6 銘柄ぶん
            self.conn.execute(
                "INSERT INTO chain_tokens(chain,address,created_ts,launchpad)"
                " VALUES('bsc',?,?,'four.meme')", (f"0x{i}", created))
            self._bucket(f"0x{i}", self.minute - 10, 3, 700.0)
        self.conn.execute(
            "INSERT INTO chain_tokens(chain,address,created_ts,launchpad)"
            " VALUES('bsc','0xlonely',?,'flap')", (created,))
        self._bucket("0xlonely", self.minute - 10, 9, 9.0)
        data = baseline.compute(self.conn, "bsc", NOW)
        self.assertEqual(baseline.cohort(data, "four.meme", 0), (3.0, 700.0))
        self.assertIsNone(baseline.cohort(data, "flap", 0))     # 1 件では平常値にしない
        self.assertIsNone(baseline.cohort(data, "four.meme", 30))

    def test_saved_baseline_is_reused_until_it_gets_old(self):
        self._bucket("0xa", self.minute - 1, 1, 1.0)
        first, made = baseline.refresh(self.conn, "bsc", NOW)
        self.assertTrue(made)
        self._bucket("0xb", self.minute - 2, 1, 1.0)
        again, made = baseline.refresh(self.conn, "bsc", NOW + 60)
        self.assertFalse(made)
        self.assertEqual(again["buckets"], first["buckets"])
        _fresh, made = baseline.refresh(self.conn, "bsc", NOW + baseline.MAX_AGE + 1)
        self.assertTrue(made)

    def test_summary_is_one_line(self):
        self.assertIn("フロアはハード下限", baseline.summary(None))
        self._bucket("0xa", self.minute - 1, 1, 1.0)
        self.assertIn("baseline buckets=1",
                      baseline.summary(baseline.compute(self.conn, "bsc", NOW)))


# --- 集計からの特徴量 --------------------------------------------------------

class TestFeatures(unittest.TestCase):
    def setUp(self):
        self.agg = agg_mod.Agg("bsc")

    def test_windows_and_ratios(self):
        a = self.agg
        a.add(Trade(NOW - 200, "buy", 50, "A", 1.0))          # 120 秒窓の外
        a.add(Trade(NOW - 100, "buy", 30, "A", 1.0))
        a.add(Trade(NOW - 100, "buy", 10, "B", 1.0))
        a.add(Trade(NOW - 20, "buy", 60, "C", 1.2))
        a.add(Trade(NOW - 10, "sell", 20, "A", 1.1))
        f = a.features("0xtok", NOW)
        self.assertEqual((f["U30"], f["B30"], f["Vb30"], f["Vs30"]), (1, 1, 60.0, 20.0))
        self.assertEqual((f["U120"], f["B120"]), (3, 3))
        self.assertAlmostEqual(f["Net120"], 100.0 - 20.0)
        self.assertAlmostEqual(f["bsi"], 80.0 / 120.0)
        self.assertEqual((f["B300"], f["U300"]), (4, 3))
        self.assertAlmostEqual(f["Top1_300"], 80.0 / 150.0)   # A が $80 で最大
        self.assertAlmostEqual(f["roundtrip_300"], 1 / 3)     # A は買って売った
        self.assertAlmostEqual(f["pc120"], 10.0)              # 1.0 → 1.1

    def test_normal_falls_back_to_the_floor_for_a_fresh_token(self):
        self.agg.add(Trade(NOW - 10, "buy", 100, "A", 1.0))
        f = self.agg.features("0xtok", NOW)
        self.assertEqual(f["mu_rate"], baseline.HARD_FLOOR_BUYERS)
        self.assertAlmostEqual(f["med"], math.log1p(baseline.HARD_FLOOR_VOL))
        self.assertEqual(f["mad"], agg_mod.MAD_FLOOR)
        self.assertFalse(f["cohort"])

    def test_cohort_is_used_while_the_token_is_young(self):
        self.agg.add(Trade(NOW - 10, "buy", 100, "A", 1.0,
                           launchpad="four.meme", created_ts=NOW - 300))
        base = {"floor": {"buyers": 0.5, "vol": 100.0},
                "cohort": {"four.meme": {"5": [6.0, 3000.0]}}}
        f = self.agg.features("0xtok", NOW, base)
        self.assertTrue(f["cohort"])
        self.assertEqual(f["mu_rate"], 6.0)
        self.assertAlmostEqual(f["med"], math.log1p(3000.0))

    def test_bundle_sticks_for_30_minutes(self):
        for i, who in enumerate("ABCDE"):
            self.agg.add(Trade(NOW - 1800, "buy", 10, who, 1.0))
        f = self.agg.features("0xtok", NOW - 1790)
        self.assertTrue(f["bundle"])
        self.assertFalse(self.agg.features("0xtok", NOW + 10)["bundle"])

    def test_bundle_only_looks_at_the_first_three_blocks(self):
        for block, ts in enumerate((NOW - 300, NOW - 240, NOW - 180, NOW - 120)):
            for who in "ABCDE":
                self.agg.add(Trade(ts, "buy", 10, who + str(block), 1.0))
        # 4 ブロック目に 5 人来ても bundle にはしない（初見 3 ブロックだけを見る）
        self.assertTrue(self.agg.features("0xtok", NOW)["bundle"])
        other = agg_mod.Agg("bsc")
        for who in "AB":
            other.add(Trade(NOW - 300, "buy", 10, who, 1.0, address="0xb"))
        for i, who in enumerate("ABCDE"):
            other.add(Trade(NOW - 120, "buy", 10, who + "x", 1.0, address="0xb"))
        self.assertTrue(other.features("0xb", NOW)["bundle"])   # 2 ブロック目はまだ対象

    def test_listed_wallets_are_counted_for_ten_minutes(self):
        self.agg.set_listed({"0xWATCHED": {"name": "Alice", "handle": None}})
        self.agg.add(Trade(NOW - 1000, "buy", 10, "0xwatched", 1.0))
        self.agg.add(Trade(NOW - 60, "buy", 10, "0xwatched", 1.0))
        self.assertEqual(self.agg.features("0xtok", NOW)["n_listed_10m"], 1)
        self.assertEqual(self.agg.features("0xtok", NOW + 1200)["n_listed_10m"], 0)

    def test_listed_wallets_are_counted_per_person(self):
        self.agg.set_listed({"0xw1": {"name": "Alice", "handle": None},
                             "0xw2": {"name": "Alice", "handle": None},
                             "0xw3": {"name": "菜就多练", "handle": None},
                             "0xw4": {"name": "売る人", "handle": None}})
        self.agg.add(Trade(NOW - 60, "buy", 10, "0xw1", 1.0))
        self.agg.add(Trade(NOW - 50, "buy", 10, "0xw2", 1.0))
        # 同じ人の 2 ウォレットが両方買っても 1 人
        self.assertEqual(self.agg.features("0xtok", NOW)["n_listed_10m"], 1)
        self.agg.add(Trade(NOW - 40, "buy", 10, "0xw3", 1.0))
        self.assertEqual(self.agg.features("0xtok", NOW)["n_listed_10m"], 2)
        self.agg.add(Trade(NOW - 30, "sell", 10, "0xw4", 1.0))
        self.assertEqual(self.agg.features("0xtok", NOW)["n_listed_10m"], 2)  # 売りは数えない

    def test_market_z_uses_the_whole_chain(self):
        for i in range(30, 5, -1):     # 毎分 1 人ずつの平常
            self.agg.add(Trade(NOW - i * 60, "buy", 10, f"A{i}", 1.0, address="0xother"))
        for who in "ABCDEFGH":         # 直前の 1 分だけ 8 人
            self.agg.add(Trade(NOW - 70, "buy", 10, who, 1.0))
        f = self.agg.features("0xtok", NOW)
        self.assertGreater(f["z_market"], 5)

    def test_accel_ring_keeps_only_a_minute(self):
        self.agg.push_g30("0xtok", NOW - 90, 1.0)
        self.agg.push_g30("0xtok", NOW - 30, 2.0)
        self.assertEqual(self.agg._g30_prev("0xtok", NOW), 2.0)
        self.assertIsNone(self.agg._g30_prev("0xtok", NOW + 120))

    def test_raised_is_dropped_when_we_did_not_watch_from_birth(self):
        late = agg_mod.Agg("bsc")
        late.add(Trade(NOW - 10, "buy", 100, "A", 1.0,
                       launchpad="four.meme", created_ts=NOW - 7200))
        self.assertIsNone(late.features("0xtok", NOW)["raised_usd"])
        early = agg_mod.Agg("bsc")
        early.add(Trade(NOW - 10, "buy", 100, "A", 1.0,
                        launchpad="four.meme", created_ts=NOW - 60))
        self.assertAlmostEqual(early.features("0xtok", NOW)["raised_usd"], 100.0)


# --- 通知の抑制と記録 --------------------------------------------------------

class TestNotify(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.cfg = _cfg()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        patch = mock.patch.object(score_log, "LOG_DIR", self.dir)
        patch.start()
        self.addCleanup(patch.stop)
        self.sent = []

    def _judge(self, score, level, now, address="0xtok", can_send=True):
        res = {"score": score, "level": level, "parts": {"vol": 1.0}, "points": {"vol": 30},
               "penalties": [], "penalty": 0.0, "reason": "", "progress": None}
        send = None
        if can_send:
            def send():
                self.sent.append((address, now))
                return True
        return score_log.judge(self.conn, self.cfg, "bsc", _feat(address=address), res,
                               now, send)

    def test_below_the_lowest_cut_is_not_notified(self):
        self.assertEqual(self._judge(50, "", NOW), "below")
        self.assertEqual(self.sent, [])

    def test_promotion_goes_through_immediately(self):
        self.assertEqual(self._judge(62, "WATCH", NOW), "notified")
        self.assertEqual(self._judge(72, "HOT", NOW + 60), "notified")
        self.assertEqual(len(self.sent), 2)

    def test_same_level_waits_for_the_cooldown_and_five_more_points(self):
        self._judge(62, "WATCH", NOW)
        self.assertEqual(self._judge(68, "WATCH", NOW + 600), "suppressed:cooldown")
        self.assertEqual(self._judge(64, "WATCH", NOW + 1801), "suppressed:cooldown")
        self.assertEqual(self._judge(67, "WATCH", NOW + 1802), "notified")

    def test_demotion_is_silent(self):
        self._judge(72, "HOT", NOW)
        self.assertEqual(self._judge(95, "WATCH", NOW + 60), "suppressed:cooldown")
        self.assertEqual(len(self.sent), 1)

    def test_there_is_no_cap_on_the_number_of_alerts(self):
        # 2026-09-19 Tomato: 多すぎるなら検知条件を直す。通数の上限で隠さない
        for i in range(30):
            self.assertEqual(self._judge(62, "WATCH", NOW + i, f"0x{i}"), "notified")
        self.assertEqual(len(self.sent), 30)

    def test_dry_run_records_but_never_sends(self):
        self.cfg["dry_run"] = True
        self.assertEqual(self._judge(95, "EXTREME", NOW), "suppressed:dry_run")
        self.assertEqual(self.sent, [])
        self.assertIsNone(db.get_score_state(self.conn, "bsc", "0xtok"))

    def test_no_sender_is_treated_as_dry_run(self):
        self.assertEqual(self._judge(95, "EXTREME", NOW, can_send=False),
                         "suppressed:dry_run")

    def test_state_survives_a_restart(self):
        """通知の記録は DB にあるので、プロセスが変わっても再通知しない。"""
        self.assertEqual(self._judge(62, "WATCH", NOW), "notified")
        state = db.get_score_state(self.conn, "bsc", "0xtok")
        self.assertEqual((state["last_level"], state["last_score"]), ("WATCH", 62))
        self.assertEqual(self._judge(63, "WATCH", NOW + 60), "suppressed:cooldown")

    def test_a_failed_send_does_not_move_the_state(self):
        def send():
            return False
        res = {"score": 95, "level": "EXTREME", "parts": {}, "points": {},
               "penalties": [], "penalty": 0.0, "reason": ""}
        self.assertEqual(
            score_log.judge(self.conn, self.cfg, "bsc", _feat(), res, NOW, send),
            "suppressed:send_failed")
        self.assertIsNone(db.get_score_state(self.conn, "bsc", "0xtok"))

    def test_every_judgement_leaves_a_line_and_a_row(self):
        self._judge(62, "WATCH", NOW)
        with open(score_log.log_path(NOW), encoding="utf-8") as f:
            lines = [json.loads(x) for x in f]
        self.assertEqual(len(lines), 1)
        entry = lines[0]
        self.assertEqual((entry["decision"], entry["level"], entry["score"]),
                         ("notified", "WATCH", 62))
        for key in ("U30", "Vb30", "mu_rate", "med", "mad", "c", "penalties", "address"):
            self.assertIn(key, entry)
        rows = self.conn.execute("SELECT * FROM scores").fetchall()
        self.assertEqual((rows[0]["level"], rows[0]["notified"]), ("WATCH", 1))
        self.assertEqual(json.loads(rows[0]["detail"])["decision"], "notified")

    def test_quiet_judgements_are_not_logged(self):
        self._judge(10, "", NOW)
        self.assertFalse(os.path.exists(score_log.log_path(NOW)))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) n FROM scores").fetchone()["n"], 0)

    def test_old_scores_are_pruned(self):
        self._judge(62, "WATCH", NOW)
        db.prune_scores(self.conn, NOW + 15 * 86400, 14)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) n FROM scores").fetchone()["n"], 0)


class TestMarks(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_mark_and_list(self):
        reply = watch.mark_command(self.conn, _cfg(), "0xABC 初動きれい", NOW)
        self.assertIn("0xabc", reply)
        watch.mark_command(self.conn, _cfg(), "rh:0xdef", NOW + 1)
        rows = db.recent_marks(self.conn)
        self.assertEqual([(r["chain"], r["address"]) for r in rows],
                         [("robinhood", "0xdef"), ("bsc", "0xabc")])
        self.assertIn("初動きれい", watch.marks_text(self.conn))

    def test_mark_without_an_address_explains_itself(self):
        self.assertIn("使い方", watch.mark_command(self.conn, _cfg(), "", NOW))

    def test_score_command_lists_the_best_of_the_last_ten_minutes(self):
        db.insert_score(self.conn, "bsc", "0xa", NOW - 60,
                        {"symbol": "AAA", "score": 81, "level": "STRONG",
                         "decision": "notified", "notified": True, "detail": "{}"})
        db.insert_score(self.conn, "bsc", "0xa", NOW - 30,
                        {"symbol": "AAA", "score": 72, "level": "HOT",
                         "decision": "below", "notified": False, "detail": "{}"})
        db.insert_score(self.conn, "bsc", "0xold", NOW - 3600,
                        {"symbol": "OLD", "score": 99, "level": "EXTREME",
                         "decision": "notified", "notified": True, "detail": "{}"})
        self.conn.commit()
        text = watch.score_text(self.conn, _cfg(), now=NOW)
        self.assertIn("81点", text)          # 銘柄ごとの最高点
        self.assertNotIn("72点", text)       # 同じ銘柄の低い方は出さない
        self.assertNotIn("OLD", text)        # 10 分より前は出さない
        self.assertIn(watch.SCORE_ICONS["STRONG"], text)  # レベルは最高点から引き直す

    def test_score_command_when_nothing_was_judged(self):
        self.assertIn("採点なし", watch.score_text(self.conn, _cfg(), now=NOW))


# --- 名簿ウォレットのエントリー ----------------------------------------------

class TestRoster(unittest.TestCase):
    """wallets.txt の読み方（`0xアドレス # 名前 | @ハンドル`）。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "wallets.txt")

    def _load(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)
        return chain_run.load_wallets(_cfg(wallets_file=self.path))

    def test_names_handles_and_bare_addresses(self):
        w = self._load("# 見出しだけの行\n"
                       "0xAAA # Alice🔶BNB | @alice\n"
                       "0xBBB # Alice🔶BNB\n"
                       "0xCCC\n"
                       "\n"
                       "0xDDD #   \n")
        self.assertEqual(set(w), {"0xaaa", "0xbbb", "0xccc", "0xddd"})
        self.assertEqual(w["0xaaa"], {"name": "Alice🔶BNB", "handle": "alice"})
        self.assertEqual(w["0xbbb"]["name"], w["0xaaa"]["name"])   # 同名＝同じ人
        self.assertIsNone(w["0xbbb"]["handle"])
        self.assertEqual(w["0xccc"], {"name": "0xccc", "handle": None})
        self.assertEqual(w["0xddd"]["name"], "0xddd")              # 名前が空ならアドレス

    def test_a_missing_file_is_empty(self):
        self.assertEqual(chain_run.load_wallets(_cfg(wallets_file=self.path + ".none")), {})


class TestListedTrades(unittest.TestCase):
    """生取引の drain → wallet_trades → 人単位の集計。"""

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.agg = agg_mod.Agg("bsc")
        self.agg.set_listed({"0xw1": {"name": "Alice", "handle": "alice"},
                             "0xw2": {"name": "Alice", "handle": None}})

    def _drain(self):
        db.insert_wallet_trades(self.conn, "bsc", self.agg.take_listed_trades())
        self.conn.commit()

    def test_drain_writes_only_listed_wallets_and_empties(self):
        self.agg.add(Trade(NOW - 600, "buy", 100.0, "0xw1", 0.001))
        self.agg.add(Trade(NOW - 300, "buy", 300.0, "0xw1", 0.003))
        self.agg.add(Trade(NOW - 60, "sell", 50.0, "0xw1", 0.004))
        self.agg.add(Trade(NOW - 30, "buy", 10.0, "0xstranger", 0.004))
        rows = self.agg.take_listed_trades()
        self.assertEqual(len(rows), 3)                        # 名簿以外は入らない
        self.assertEqual(self.agg.take_listed_trades(), [])   # 取ったら空
        db.insert_wallet_trades(self.conn, "bsc", rows)
        self.conn.commit()
        r = db.wallet_entries(self.conn, "bsc", "0xtok", NOW - 86400)
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["wallet"], r[0]["n_buys"], r[0]["n_sells"]), ("0xw1", 2, 1))
        self.assertAlmostEqual(r[0]["buy_usd"], 400.0)
        self.assertAlmostEqual(r[0]["sell_usd"], 50.0)
        self.assertEqual(r[0]["first_buy_ts"], NOW - 600)
        # 平均取得単価 = 400 / (100/0.001 + 300/0.003) = $0.002
        self.assertAlmostEqual(r[0]["buy_qty"], 200000.0)
        self.assertAlmostEqual(r[0]["buy_usd"] / r[0]["buy_qty"], 0.002)

    def test_two_wallets_roll_up_into_one_person(self):
        self.agg.add(Trade(NOW - 600, "buy", 100.0, "0xw2", 0.001))
        self.agg.add(Trade(NOW - 300, "buy", 900.0, "0xw1", 0.001))
        self._drain()
        people = chain_run.listed_entries(self.conn, "bsc", "0xtok", NOW, self.agg)
        self.assertEqual(len(people), 1)
        p = people[0]
        self.assertEqual((p["name"], p["handle"]), ("Alice", "alice"))
        self.assertAlmostEqual(p["buy_usd"], 1000.0)
        self.assertEqual(p["wallet"], "0xw1")            # リンクは買い額の大きい方
        self.assertEqual(p["first_buy_ts"], NOW - 600)   # 初回は 2 ウォレットの早い方
        self.assertEqual(p["n_buys"], 2)

    def test_only_the_last_24h_is_shown(self):
        self.agg.add(Trade(NOW - 90000, "buy", 100.0, "0xw1", 0.001))
        self._drain()
        self.assertEqual(
            chain_run.listed_entries(self.conn, "bsc", "0xtok", NOW, self.agg), [])

    def test_prune_drops_rows_past_the_retention(self):
        db.insert_wallet_trades(self.conn, "bsc", [
            ("0xw1", "0xtok", NOW - 20 * 86400, "buy", 1.0, 1.0, 1),
            ("0xw1", "0xtok", NOW - 60, "buy", 1.0, 1.0, 2)])
        db.prune_wallet_trades(self.conn, NOW, 14)
        self.conn.commit()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM wallet_trades").fetchone()["n"], 1)


class TestScoreMessage(unittest.TestCase):
    """通知本文に出る名簿の行。"""

    def _row(self):
        return {"chain": "bsc", "address": "0xtok", "symbol": "SYM", "price": 0.0000123,
                "liquidity": 50000.0, "mcap": 500000.0, "launchpad": None,
                "created_ts": NOW - 960}

    def _res(self, entries=None):
        return {"level": "HOT", "score": 72.0, "points": {}, "parts": {}, "penalties": [],
                "penalty": 0.0, "progress": None, "entries": entries or []}

    def _line(self, entry):
        lines = watch.build_score_message(self._row(), self._res([entry]), _feat(), NOW).split("\n")
        return lines[lines.index("👛 入ってる人（24h）") + 1]

    def test_an_open_position_shows_the_average_entry_price(self):
        line = self._line({"name": "Alice<b>", "handle": "alice", "wallet": "0xw1",
                           "buy_usd": 1230.0, "sell_usd": 0.0, "buy_qty": 1e8,
                           "first_buy_ts": NOW - 720, "n_buys": 2, "n_sells": 0})
        # 名前は html.escape。隠しリンク（<a href>）は付けない＝アプリの「開きますか」を出さない
        self.assertEqual(line, "Alice&lt;b&gt; @alice ・ 買 $1,230 ・ 平均 $0.0000123 ・ 初回 12 分前")

    def test_a_wallet_that_sold_everything_is_marked(self):
        line = self._line({"name": "菜", "handle": None, "wallet": "0xw2",
                           "buy_usd": 500.0, "sell_usd": 800.0, "buy_qty": 0.0,
                           "first_buy_ts": NOW - 30, "n_buys": 1, "n_sells": 1})
        # 数量が取れなければ平均単価は出さない。売りは 0 より大きいときだけ出す
        self.assertEqual(line, "菜 ・ 買 $500 ・ 売 $800 ・ 初回 1 分未満 ・ 売り抜け済み")

    def test_no_entries_means_no_line(self):
        text = watch.build_score_message(self._row(), self._res(), _feat(), NOW)
        self.assertNotIn("👛", text)

    def test_body_has_raw_links_and_no_breakdown(self):
        lines = watch.build_score_message(self._row(), self._res(), _feat(), NOW).split("\n")
        self.assertEqual(lines[0], "<b>🔥 HOT 72点  $SYM</b>")
        self.assertEqual(lines[1], "BSC ・ 作成 16 分前")
        self.assertIn("時価総額 $500K", lines)
        self.assertIn("流動性 $50K", lines)
        # 内訳は本文に出さない（JSONL / scores.detail にある）。リンクは生 URL を 1 行 1 本
        self.assertFalse(any("内訳" in ln or "<a href" in ln for ln in lines))
        self.assertEqual(lines[-3:], ["https://gmgn.ai/bsc/token/0xtok",
                                      "https://dexscreener.com/bsc/0xtok",
                                      "<code>0xtok</code>"])

    def test_security_and_crowd_lines_when_cached(self):
        text = watch.build_score_message(self._row(), self._res(), _feat(), NOW)
        self.assertNotIn("安全", text)             # 未取得なら行ごと省く
        sec = {"is_honeypot": "0", "buy_tax": "0", "sell_tax": "0.05", "holder_count": "231"}
        row = dict(self._row(), top10_rate=0.45, sniper_count=3)
        lines = watch.build_score_message(row, self._res(), _feat(), NOW, sec).split("\n")
        self.assertIn("保有者 231 ・ 上位10 45% ・ sniper 3", lines)
        self.assertIn("安全 honeypot なし ・ 税 買0%/売5%", lines)


if __name__ == "__main__":
    unittest.main()


class PruneLogsTest(unittest.TestCase):
    def test_removes_only_older_than_retention(self):
        with tempfile.TemporaryDirectory() as d:
            old_dir, score_log.LOG_DIR = score_log.LOG_DIR, d
            try:
                now = time.mktime((2026, 9, 19, 12, 0, 0, 0, 0, -1))
                for name in ("pump-score-20260901.jsonl", "pump-score-20260905.jsonl",
                             "pump-score-20260919.jsonl", "other.log"):
                    open(os.path.join(d, name), "w").close()
                self.assertEqual(score_log.prune_logs(now, 14), 1)   # 09-01 だけ 14 日より前
                self.assertEqual(sorted(os.listdir(d)),
                                 ["other.log", "pump-score-20260905.jsonl",
                                  "pump-score-20260919.jsonl"])
            finally:
                score_log.LOG_DIR = old_dir


class TestTimeline(unittest.TestCase):
    """通知 1 通ぶんの時系列（作成 → 初観測 → 上昇開始 → 到達 → 判定 → 送信 → TG 受理）。"""

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.cfg = _cfg()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        patch = mock.patch.object(score_log, "LOG_DIR", self.dir)
        patch.start()
        self.addCleanup(patch.stop)

    def snaps(self, prices, step=60, end=NOW - 60):
        """古い順に price を並べたスナップショット列（末尾が end）。"""
        return [{"ts": end - step * (len(prices) - 1 - i), "price": p}
                for i, p in enumerate(prices)]

    def score(self, ts, value):
        db.insert_score(self.conn, "bsc", "0xtok", ts,
                        {"symbol": "SYM", "score": value, "level": "", "decision": "log",
                         "notified": False, "detail": ""})
        self.conn.commit()

    def judge(self, sent, now=NOW, value=72.0, level="HOT", extra=None):
        res = {"score": value, "level": level, "parts": {}, "points": {},
               "penalties": [], "penalty": 0.0, "reason": "", "progress": None}
        return score_log.judge(self.conn, self.cfg, "bsc", _feat(), res, now,
                               lambda: sent, extra)

    # --- 上昇開始 -----------------------------------------------------------

    def test_rise_start_is_the_bottom_of_the_current_run(self):
        snaps = self.snaps([1.0, 0.5, 0.6, 0.9])     # 一度大きく落ちてから上げた
        self.assertEqual(score_log.rise_start(snaps, NOW),
                         (snaps[1]["ts"], 0.5))

    def test_rise_start_falls_back_to_the_head_of_the_window(self):
        snaps = self.snaps([0.1, 0.2, 0.3])          # ずっと上げっぱなし
        self.assertEqual(score_log.rise_start(snaps, NOW)[0], snaps[0]["ts"])

    def test_rise_start_tolerates_a_small_dip(self):
        snaps = self.snaps([0.10, 0.20, 0.198, 0.30])   # 1% の押し目は上昇の続き
        self.assertEqual(score_log.rise_start(snaps, NOW)[1], 0.10)

    def test_rise_start_ignores_snapshots_outside_the_window(self):
        old = [{"ts": NOW - score_log.RISE_WINDOW - 1, "price": 0.01}]
        self.assertEqual(score_log.rise_start(old + self.snaps([0.2, 0.3]), NOW)[1], 0.2)

    def test_rise_start_without_usable_snapshots(self):
        self.assertEqual(score_log.rise_start([], NOW), (None, None))
        self.assertEqual(score_log.rise_start([{"ts": NOW - 60, "price": None}], NOW),
                         (None, None))

    # --- 初観測 ---------------------------------------------------------------

    def test_first_seen_takes_the_earliest_of_lane_and_db(self):
        # tokens.first_seen_ts は初めて判定を通った時刻。集計が見た最初の取引の方が早ければそちら
        db.upsert_token(self.conn, "bsc", "0xtok", {"symbol": "SYM"}, NOW - 100, True)
        res = {"score": 72.0, "level": "HOT"}
        tl = score_log.timeline(self.conn, self.cfg, "bsc", _feat(), res, NOW,
                                {"first_seen_ts": NOW - 900}, {"ok": True})
        self.assertEqual(tl["first_seen_ts"], NOW - 900)
        tl = score_log.timeline(self.conn, self.cfg, "bsc", _feat(), res, NOW, {}, {"ok": True})
        self.assertEqual(tl["first_seen_ts"], NOW - 100)

    # --- 到達 / 初採点 ------------------------------------------------------

    def test_reach_ts_is_the_first_row_at_or_above_the_cut(self):
        self.score(NOW - 300, 55.0)
        self.score(NOW - 120, 72.0)
        self.score(NOW - 60, 75.0)
        self.assertEqual(score_log.reach_ts(self.conn, "bsc", "0xtok", 70, NOW), NOW - 120)
        # 初採点は点数を問わない最初の行
        self.assertEqual(db.first_score_ts(self.conn, "bsc", "0xtok", NOW - 3600, 0),
                         NOW - 300)

    def test_reach_ts_does_not_look_before_the_rise_started(self):
        # 前の波で届いた 5 分前は数えず、上昇開始（2 分前）以降の最初の行
        self.score(NOW - 300, 72.0)
        self.score(NOW - 60, 75.0)
        self.assertEqual(score_log.reach_ts(self.conn, "bsc", "0xtok", 70, NOW,
                                            since=NOW - 120), NOW - 60)
        self.assertEqual(score_log.reach_ts(self.conn, "bsc", "0xtok", 70, NOW,
                                            since=NOW - 30), NOW)

    def test_reach_ts_is_now_when_nothing_reached_it_before(self):
        self.score(NOW - 300, 55.0)
        self.assertEqual(score_log.reach_ts(self.conn, "bsc", "0xtok", 70, NOW), NOW)

    def test_first_score_ts_ignores_rows_outside_the_window(self):
        self.score(NOW - score_log.REACH_WINDOW - 1, 99.0)
        self.assertIsNone(db.first_score_ts(self.conn, "bsc", "0xtok",
                                            NOW - score_log.REACH_WINDOW, 0))

    # --- alerts への記録 ----------------------------------------------------

    def test_a_notified_judgement_writes_one_alert_row(self):
        db.upsert_token(self.conn, "bsc", "0xtok", {"symbol": "SYM",
                                                    "creation_ts": NOW - 600}, NOW - 500)
        self.conn.commit()
        self.score(NOW - 120, 72.0)
        sent = {"ok": True, "photo": True, "message_id": 812, "tg_date": NOW,
                "send_start_ts": NOW + 0.2, "sent_ts": NOW + 0.5, "error": None}
        self.judge(sent, extra={"lane": "chain", "snapshots": self.snaps([0.1, 0.3]),
                                "data_ts": NOW - 2.0, "price": 0.3})
        rows = db.recent_alerts(self.conn, "bsc", "0xtok")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["symbol"], r["lane"], r["level"], r["score"]),
                         ("SYM", "chain", "HOT", 72.0))
        self.assertEqual((r["created_ts"], r["first_seen_ts"], r["reach_ts"],
                          r["judged_ts"]), (NOW - 600, NOW - 500, NOW - 120, NOW))
        self.assertEqual((r["message_id"], r["photo"], r["ok"], r["tg_date"]),
                         (812, 1, 1, NOW))
        self.assertEqual((r["send_start_ts"], r["sent_ts"], r["judged_price"]),
                         (NOW + 0.2, NOW + 0.5, 0.3))
        self.assertEqual(json.loads(r["detail"])["lane"], "chain")   # snapshots は入れない
        self.assertNotIn("snapshots", json.loads(r["detail"]))

    def test_a_failed_send_is_recorded_too(self):
        self.judge({"ok": False, "error": "Bad Request", "photo": False})
        r = db.recent_alerts(self.conn, "bsc", "0xtok")[0]
        self.assertEqual((r["ok"], r["photo"], r["error"]), (0, 0, "Bad Request"))

    def test_a_plain_true_from_send_still_records(self):
        self.assertEqual(self.judge(True), "notified")
        r = db.recent_alerts(self.conn, "bsc", "0xtok")[0]
        self.assertEqual((r["ok"], r["photo"], r["message_id"]), (1, 0, None))

    def test_nothing_is_recorded_when_we_do_not_notify(self):
        self.cfg["dry_run"] = True
        self.judge(True)
        self.assertEqual(db.recent_alerts(self.conn, "bsc", "0xtok"), [])

    def test_recent_alerts_are_newest_first_and_limited(self):
        for i, level in enumerate(("WATCH", "HOT", "STRONG")):   # 昇格は毎回通る
            self.judge(True, now=NOW + i * 3600, value=62.0 + i * 10, level=level)
        rows = db.recent_alerts(self.conn, "bsc", "0xtok", limit=2)
        self.assertEqual([r["judged_ts"] for r in rows], [NOW + 7200, NOW + 3600])
        # chain 省略でも同じ行が拾える
        self.assertEqual(len(db.recent_alerts(self.conn, None, "0xtok", limit=5)), 3)

    def test_prune_alerts_drops_the_old_ones(self):
        self.judge(True, now=NOW - 20 * 86400, value=62.0, level="WATCH")
        self.judge(True, now=NOW)
        db.prune_alerts(self.conn, NOW, 14)
        rows = db.recent_alerts(self.conn, "bsc", "0xtok")
        self.assertEqual([r["judged_ts"] for r in rows], [NOW])

    def test_the_jsonl_row_carries_the_timeline(self):
        self.judge({"ok": True, "message_id": 5})
        with open(score_log.log_path(NOW)) as f:
            rows = [json.loads(line) for line in f]
        tl = rows[-1]["timeline"]
        self.assertEqual((tl["judged_ts"], tl["message_id"], tl["ok"]), (NOW, 5, 1))
        self.assertNotIn("snapshots", rows[-1])   # 特徴量ごと肥大させない

    # --- 1 行の表示 ---------------------------------------------------------

    def test_the_log_line_shows_each_gap_from_the_previous_stage(self):
        row = {"symbol": "SYM", "level": "HOT", "score": 72.0,
               "created_ts": NOW, "first_seen_ts": NOW + 35, "rise_start_ts": NOW + 525,
               "reach_ts": NOW + 666, "judged_ts": NOW + 668,
               "send_start_ts": NOW + 669.1, "sent_ts": NOW + 669.4,
               "photo": 1, "message_id": 812}
        head = time.strftime("%H:%M:%S", time.localtime(NOW))
        self.assertEqual(
            score_log.timeline_line(row),
            f"alert SYM HOT 72 作成 {head} → 初観測 +35s → 上昇開始 +8m10s →"
            " 到達 +2m21s → 判定 +2s → 送信 +1.1s → TG受理 +0.3s 画像=○ id=812")

    def test_missing_stages_become_question_marks(self):
        row = {"symbol": "SYM", "level": "WATCH", "score": 62.0, "judged_ts": NOW,
               "sent_ts": NOW + 1, "photo": 0, "error": "timeout"}
        head = time.strftime("%H:%M:%S", time.localtime(NOW))
        self.assertEqual(
            score_log.timeline_line(row),
            f"alert SYM WATCH 62 作成 ? → 初観測 ? → 上昇開始 ? → 到達 ? →"
            f" 判定 {head} → 送信 ? → TG受理 +1s 画像=× err=timeout")
