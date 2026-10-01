"""データ源アダプタ（GeckoTerminal / DexScreener）の整形と、源のマージ・予算配分を検証する。

ネットワークは一切叩かない。応答の形は 2026-09-18 に実 API から取った値をそのまま使っている。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rules  # noqa: E402
import watch  # noqa: E402
from sources import dexscreener, geckoterminal  # noqa: E402

# GeckoTerminal networks/bsc/new_pools?page=1 の 1 プール（four-meme の打ち上げ直後）
GT_POOL = {
    "id": "bsc_0xpool",
    "type": "pool",
    "attributes": {
        "name": "NEWTON / WBNB",
        "base_token_price_usd": "0.0000407580239310747",
        "market_cap_usd": None,                      # GT は null が多いので FDV で代用する
        "fdv_usd": "40758.02393",
        "reserve_in_usd": "12399.9534163181",
        "pool_created_at": "2026-09-18T00:41:08Z",
        "volume_usd": {"m5": "3863.761522358", "h1": "3863.7", "h24": "3863.7"},
        "transactions": {"m5": {"buys": 14, "sells": 8, "buyers": 13, "sellers": 7}},
        "price_change_percentage": {"m5": "74.068", "h1": "74.068", "h24": "74.068"},
    },
    "relationships": {
        "base_token": {"data": {"id": "bsc_0xFA52559bdb9433f675919e340b1f878fb76F009b"}},
        "dex": {"data": {"id": "four-meme", "type": "dex"}},
    },
}

# DexScreener tokens/v1/bsc/<addr> の 1 ペア
DS_PAIR = {
    "chainId": "bsc", "dexId": "pancakeswap",
    "priceUsd": "0.002391", "marketCap": 2391620, "fdv": 2391620,
    "liquidity": {"usd": 245071.78},
    "volume": {"h24": 59677.04, "h1": 2060.52, "m5": 8.84},
    "txns": {"m5": {"buys": 2, "sells": 0}, "h1": {"buys": 35, "sells": 5}},
    "priceChange": {"m5": -0.07, "h1": 0.32, "h24": -12.8},
    "pairCreatedAt": 1779343795000,
    "baseToken": {"address": "0x0598075dc4d1c9484DAa80C62313AAD39fc77777",
                  "name": "Test Liquid Staking", "symbol": "TLS"},
}


class TestGeckoTerminal(unittest.TestCase):
    def test_row(self):
        r = geckoterminal._row("bsc", GT_POOL)
        self.assertEqual(r["address"], "0xfa52559bdb9433f675919e340b1f878fb76f009b")  # 小文字化
        self.assertEqual(r["symbol"], "NEWTON")        # name は "SYM / QUOTE"
        self.assertEqual(r["source"], "gt")
        self.assertEqual(r["dex"], "four-meme")
        self.assertAlmostEqual(r["mcap"], 40758.02393)
        self.assertTrue(r["mcap_is_fdv"])              # market_cap_usd が null なので FDV 代用
        self.assertAlmostEqual(r["liquidity"], 12399.9534163181)
        self.assertAlmostEqual(r["vol5m"], 3863.761522358)
        self.assertEqual((r["buys5m"], r["sells5m"], r["buyers5m"]), (14.0, 8.0, 13.0))
        self.assertAlmostEqual(r["pc5m"], 74.068)
        self.assertEqual(r["created_ts"], 1789692068)  # ISO8601(UTC) → unix 秒
        self.assertIsNone(r["holders"])                # GT は保有者数を持たない

    def test_row_keys_are_complete(self):
        r = geckoterminal._row("bsc", GT_POOL)
        for k in rules.ROW_KEYS:
            self.assertIn(k, r)

    def test_missing_base_token(self):
        self.assertIsNone(geckoterminal._row("bsc", {"attributes": {}}))

    def test_epoch_timestamp_also_accepted(self):
        self.assertEqual(geckoterminal._ts(1789692068), 1789692068)
        self.assertIsNone(geckoterminal._ts("いつか"))


class TestDexScreener(unittest.TestCase):
    def test_row(self):
        r = dexscreener._row("bsc", DS_PAIR)
        self.assertEqual(r["address"], "0x0598075dc4d1c9484daa80c62313aad39fc77777")
        self.assertEqual((r["symbol"], r["source"]), ("TLS", "ds"))
        self.assertEqual(r["mcap"], 2391620)
        self.assertFalse(r["mcap_is_fdv"])             # marketCap があるので FDV 代用ではない
        self.assertEqual(r["vol5m"], 8.84)
        self.assertEqual((r["buys5m"], r["sells5m"]), (2.0, 0.0))
        self.assertIsNone(r["buyers5m"])               # DS は買い手の実数を持たない
        self.assertEqual(r["created_ts"], 1779343795.0)  # ミリ秒 → 秒
        self.assertIsNone(r["visiting"])

    def test_fdv_fallback(self):
        pair = dict(DS_PAIR, marketCap=None)
        r = dexscreener._row("bsc", pair)
        self.assertEqual(r["mcap"], 2391620)
        self.assertTrue(r["mcap_is_fdv"])

    def test_best_pair_wins(self):
        thin = dict(DS_PAIR, liquidity={"usd": 10.0}, priceUsd="0.9")
        rows = [dexscreener._row("bsc", thin), dexscreener._row("bsc", DS_PAIR)]
        best = dexscreener._best(rows)
        self.assertEqual(len(best), 1)
        self.assertAlmostEqual(best[0]["liquidity"], 245071.78)


class TestMergeSources(unittest.TestCase):
    def rows(self):
        ds = {k: None for k in rules.ROW_KEYS}
        ds.update({"chain": "bsc", "address": "0xa", "source": "ds", "price": 2.0,
                   "mcap": 200.0, "mcap_is_fdv": False, "vol5m": 500.0, "buys5m": 9.0,
                   "created_ts": 111.0, "symbol": "DS"})
        gt = {k: None for k in rules.ROW_KEYS}
        gt.update({"chain": "bsc", "address": "0xa", "source": "gt", "price": 3.0,
                   "mcap": 300.0, "mcap_is_fdv": True, "buyers5m": 7.0,
                   "created_ts": 222.0, "symbol": "GT"})
        gm = {k: None for k in rules.ROW_KEYS}
        gm.update({"chain": "bsc", "address": "0xa", "source": "gmgn", "price": 4.0,
                   "holders": 120.0, "smart": 5.0, "visiting": 900.0, "wash": False,
                   "created_ts": 333.0, "symbol": "GM", "launchpad": "four.meme"})
        return {"ds": ds, "gt": gt, "gmgn": gm}

    def test_price_prefers_dexscreener(self):
        m = watch.merge_sources(self.rows())
        self.assertEqual(m["price"], 2.0)
        self.assertEqual(m["source"], "ds+gt+gmgn")

    def test_token_facts_prefer_gmgn(self):
        m = watch.merge_sources(self.rows())
        self.assertEqual(m["created_ts"], 333.0)   # GMGN の作成時刻（GT/DS はプール生成時刻）
        self.assertEqual(m["symbol"], "GM")
        self.assertEqual((m["holders"], m["smart"], m["visiting"]), (120.0, 5.0, 900.0))

    def test_fills_gaps_from_lower_priority(self):
        m = watch.merge_sources(self.rows())
        self.assertEqual(m["buyers5m"], 7.0)       # GT にしかない
        self.assertEqual(m["vol5m"], 500.0)        # DS にしかない

    def test_mcap_flag_follows_its_source(self):
        rows = self.rows()
        m = watch.merge_sources(rows)
        self.assertEqual((m["mcap"], m["mcap_is_fdv"]), (200.0, False))
        rows["ds"]["mcap"] = None                  # DS が時価総額を持たなければ GT の FDV
        m = watch.merge_sources(rows)
        self.assertEqual((m["mcap"], m["mcap_is_fdv"]), (300.0, True))

    def test_single_source(self):
        rows = self.rows()
        m = watch.merge_sources({"gmgn": rows["gmgn"]})
        self.assertEqual((m["price"], m["source"]), (4.0, "gmgn"))


class TestGtPlan(unittest.TestCase):
    CHAINS = ["bsc", "robinhood", "arc"]

    def test_one_call_per_cycle(self):
        # 上限は間隔でも効く（12 秒に 1 回）ので、1 サイクル 1 コールに抑える＝3/分
        for s in range(12):
            self.assertEqual(len(watch.gt_plan(self.CHAINS, s)), 1)

    def test_two_minute_rotation(self):
        flat = [c for s in range(6) for c in watch.gt_plan(self.CHAINS, s)]
        self.assertEqual(flat.count(("trending", "bsc")), 3)
        self.assertEqual(flat.count(("new", "bsc")), 1)
        self.assertEqual(flat.count(("trending", "robinhood")), 1)
        self.assertEqual(flat.count(("trending", "arc")), 1)

    def test_top_volume_every_four_minutes(self):
        flat = [c for s in range(12) for c in watch.gt_plan(self.CHAINS, s)]
        self.assertEqual(flat.count(("h24", "bsc")), 1)

    def test_single_chain(self):
        plans = [watch.gt_plan(["bsc"], s) for s in range(12)]
        self.assertTrue(all(all(c[1] == "bsc" for c in p) for p in plans))

    def test_no_chains(self):
        self.assertEqual(watch.gt_plan([], 0), [])


class TestGtBudget(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        import db
        self.tmp = tempfile.mkdtemp()
        self.conn = db.connect(os.path.join(self.tmp, "state.db"))
        self.cfg = {"gt": {"max_calls_per_min": 5, "min_gap_seconds": 12,
                           "block_seconds": 60}}
        self.cleanup = lambda: shutil.rmtree(self.tmp, ignore_errors=True)

    def tearDown(self):
        self.conn.close()
        self.cleanup()

    def test_min_gap_between_calls(self):
        # 分 5 コールに収まっていても、続けて撃つと 429 になる（2026-09-18 実測）
        now = 1_800_000_000
        self.assertTrue(watch.gt_take(self.conn, self.cfg, now))
        self.assertFalse(watch.gt_take(self.conn, self.cfg, now + 1))
        self.assertFalse(watch.gt_take(self.conn, self.cfg, now + 11))
        self.assertTrue(watch.gt_take(self.conn, self.cfg, now + 12))

    def test_min_gap_keeps_it_to_five_per_minute(self):
        now = 1_800_000_000
        got = [watch.gt_take(self.conn, self.cfg, now + 12 * i) for i in range(5)]
        self.assertEqual(got, [True] * 5)             # 12 秒間隔なら 1 分に 5 発が上限
        self.assertEqual(watch.gt_used(self.conn, now), 5)

    def test_per_minute_cap_is_the_backstop(self):
        # 間隔の条件を外しても分 5 コールで止まる（設定を緩めたときの保険）
        cfg = {"gt": {"max_calls_per_min": 5, "min_gap_seconds": 0, "block_seconds": 60}}
        now = 1_800_000_000
        self.assertEqual([watch.gt_take(self.conn, cfg, now) for _ in range(6)],
                         [True] * 5 + [False])
        self.assertTrue(watch.gt_take(self.conn, cfg, now + 60))   # 次の分で戻る
        self.assertEqual(watch.gt_used(self.conn, now + 60), 1)

    def test_block_after_429(self):
        import db
        now = 1_800_000_000
        db.kv_set(self.conn, "gt_block_until", now + 60)
        self.assertFalse(watch.gt_take(self.conn, self.cfg, now))
        self.assertFalse(watch.gt_take(self.conn, self.cfg, now + 59))
        self.assertTrue(watch.gt_take(self.conn, self.cfg, now + 60))


if __name__ == "__main__":
    unittest.main()
