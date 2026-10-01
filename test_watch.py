"""watch.py の設定変更（/set）・近かった銘柄（/near）・1 銘柄 1 通の通知を検証する。

GMGN も GoPlus も Telegram も呼ばない（安全確認はモジュール全体で「未取得」に固定）。
しきい値は実 config.json ではなくここの CFG に固定する
（本人が /set や /dryrun でしきい値を変えた瞬間にテストが落ちないように）。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import db  # noqa: E402
import notify  # noqa: E402
import watch  # noqa: E402

CFG = {
    "chains": ["bsc"],
    "dry_run": True,
    "filters": {"min_liquidity_usd": 10000, "max_market_cap_usd": 500000000,
                "min_holders": 30,
                "size_any": {"mcap": 100000, "vol24h": 300000, "vol5m": 30000}},
    "s1": {"pc1h_pct": 80, "pc5m_pct": 25, "min_buys_5m": 30, "cooldown_h": 2},
    "s2": {"min_vol5m_usd": 20000, "baseline_mult": 3, "baseline_from_min": 15,
           "baseline_to_min": 75, "no_baseline_vol5m_usd": 60000, "cooldown_h": 2},
    "s3": {"min_buys_5m": 300, "min_buyers_5m": 150, "buy_sell_ratio": 1.0, "cooldown_h": 2},
    "s4": {"window_min": 30, "pct": 15, "abs": 30, "cooldown_h": 2},
    "s5": {"min_smart": 5, "window_min": 30, "min_increase": 3, "cooldown_h": 6},
    "s6": {"window_min": 30, "ratio": 2, "min_visiting": 300, "cooldown_h": 6},
    "m1": {"ladder": [250000, 500000, 1000000, 3000000, 10000000, 30000000, 100000000],
           "max_age_days": 30},
    "m2": {"ladder": [3, 10, 30, 100]},
    "m3": {"ladder": [700000, 1000000, 3000000, 10000000, 30000000, 100000000],
           "max_age_days": 30},
    "m4": {"min_age_days": 30, "min_change_24h_pct": 200, "cooldown_h": 24},
    "milestone": {"min_pc1h_pct": 0, "min_vol5m_usd": 2000, "min_buys_5m": 5},
    "yabai": {"mcap": 1000000, "volume": 1000000, "x": 10, "heat": 4},
    "notify": {"immediate": {"mcap": 300000, "vol24h": 700000},
               "immediate_cooldown_min": 10, "batch_minutes": 5, "max_pending": 15},
    "gt": {"enabled": True, "max_calls_per_min": 5, "min_gap_seconds": 12,
           "block_seconds": 60, "trending_duration": "5m"},
    "ds": {"enabled": True, "workers": 6, "active_hours": 24},
    "gmgn": {"enabled": True, "interval_24h_every": 3},
    "snapshot_min_gap_seconds": 90,
    "snapshot_retention_days": 2,
}


def cfg():
    return json.loads(json.dumps(CFG))


_NO_SECURITY = mock.patch.object(watch.enrich, "security", return_value=None)


def setUpModule():
    _NO_SECURITY.start()


def tearDownModule():
    _NO_SECURITY.stop()


class TestSetConfigValue(unittest.TestCase):
    def test_number(self):
        c = cfg()
        ok, msg, up = watch.set_config_value(c, "s3.min_buys_5m", "200")
        self.assertTrue(ok)
        self.assertEqual(msg, "s3.min_buys_5m: 300 → 200")
        self.assertEqual(c["s3"]["min_buys_5m"], 200)
        self.assertEqual(up["s3"]["min_buys_5m"], 200)

    def test_list(self):
        c = cfg()
        ok, _, up = watch.set_config_value(c, "m3.ladder", "500000,1000000,3000000")
        self.assertTrue(ok)
        self.assertEqual(c["m3"]["ladder"], [500000, 1000000, 3000000])
        self.assertEqual(up["m3"]["ladder"], [500000, 1000000, 3000000])

    def test_bool(self):
        c = cfg()
        ok, msg, _ = watch.set_config_value(c, "dry_run", "off")
        self.assertTrue(ok)
        self.assertFalse(c["dry_run"])
        self.assertEqual(msg, "dry_run: on → off")

    def test_float(self):
        c = cfg()
        ok, _, _ = watch.set_config_value(c, "s2.baseline_mult", "2.5")
        self.assertTrue(ok)
        self.assertEqual(c["s2"]["baseline_mult"], 2.5)

    def test_top_level_number(self):
        c = cfg()
        ok, _, up = watch.set_config_value(c, "snapshot_min_gap_seconds", "60")
        self.assertTrue(ok)
        self.assertEqual(up, {"snapshot_min_gap_seconds": 60})

    def test_unknown_key(self):
        c = cfg()
        ok, msg, up = watch.set_config_value(c, "s3.nope", "1")
        self.assertFalse(ok)
        self.assertIsNone(up)
        self.assertIn("知らないキー", msg)
        self.assertEqual(c["s3"]["min_buys_5m"], 300)

    def test_unknown_section(self):
        ok, _, _ = watch.set_config_value(cfg(), "t3.min_buys_5m", "1")
        self.assertFalse(ok)

    def test_section_itself_rejected(self):
        ok, _, _ = watch.set_config_value(cfg(), "s3", "1")
        self.assertFalse(ok)

    def test_type_mismatch_string(self):
        c = cfg()
        ok, msg, up = watch.set_config_value(c, "s3.min_buys_5m", "abc")
        self.assertFalse(ok)
        self.assertIsNone(up)
        self.assertIn("型が違う", msg)
        self.assertEqual(c["s3"]["min_buys_5m"], 300)

    def test_type_mismatch_list_for_number(self):
        c = cfg()
        ok, _, _ = watch.set_config_value(c, "s3.min_buys_5m", "1,2")
        self.assertFalse(ok)
        self.assertEqual(c["s3"]["min_buys_5m"], 300)

    def test_type_mismatch_number_for_bool(self):
        c = cfg()
        ok, _, _ = watch.set_config_value(c, "dry_run", "1")
        self.assertFalse(ok)
        self.assertTrue(c["dry_run"])

    def test_chains_delegated(self):
        ok, msg, _ = watch.set_config_value(cfg(), "chains", "bsc")
        self.assertFalse(ok)
        self.assertIn("/chains", msg)

    def test_config_text_lists_dotted_keys(self):
        text = watch.config_text(cfg())
        self.assertIn("s3.min_buys_5m=300", text)
        self.assertIn("m3.ladder=700000,1000000,3000000,10000000,30000000,100000000", text)
        self.assertIn("filters.min_holders=30", text)
        self.assertIn("dry_run=on", text)

    def test_real_config_has_every_key(self):
        """config.json と DEFAULTS のキーがずれていない（/set が効かないキーを作らない）。"""
        real = watch.load_config()

        def same(a, b, path):
            self.assertEqual(set(a), set(b), f"{path} のキーがずれている")
            for k, v in a.items():
                if isinstance(v, dict):
                    same(v, b[k], f"{path}.{k}" if path else k)

        same(watch.DEFAULTS, {k: real[k] for k in watch.DEFAULTS}, "")


class TestNearReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.connect(os.path.join(self.tmp, "state.db"))
        self.now = int(time.time())
        self.cfg = cfg()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, addr, symbol, snaps, fired_trigger=None, first_price=1.0):
        self.conn.execute(
            "INSERT INTO tokens(chain,address,symbol,creation_ts,first_seen_ts,first_price)"
            " VALUES(?,?,?,?,?,?)",
            ("bsc", addr, symbol, self.now - 7200, self.now - 7200, first_price))
        for mins, s in snaps:
            db.insert_snapshot(self.conn, "bsc", addr, self.now - mins * 60, s)
        if fired_trigger:
            self.conn.execute(
                'INSERT INTO events(ts,chain,address,"trigger",stage,value,heat,yabai,message,'
                "notified,sent) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (self.now - 600, "bsc", addr, fired_trigger, "", 1.0, 1, 0, "通知済み", 1, 0))
        self.conn.commit()

    def test_rates_per_trigger(self):
        self.add("0xaaa111", "NEAR", [
            (35, {"mcap": 150_000, "price": 1.0, "holders": 1_000, "smart": 2, "visiting": 100,
                  "buys5m": 120, "sells5m": 100, "vol5m": 5_000, "vol24h": 400_000,
                  "pc1h": 10.0, "pc5m": 1.0}),
            (2, {"mcap": 180_000, "price": 1.5, "holders": 1_100, "smart": 4, "visiting": 150,
                 "buys5m": 200, "sells5m": 100, "vol5m": 12_000, "vol24h": 520_000,
                 "pc1h": 40.0, "pc5m": 5.0}),
        ])
        text = watch.near_report(self.conn, self.cfg, 6, now=self.now)
        for expect in (
            "$NEAR 1h +40% (50%) 0xaaa1",
            "$NEAR 5 分 $12K（平常 $5K） (60%) 0xaaa1",
            "$NEAR 買 200 / 売 100 (67%) 0xaaa1",
            "$NEAR +10%/30 分（1,000→1,100） (67%) 0xaaa1",
            "$NEAR +2（今 4） (67%) 0xaaa1",
            "$NEAR 150（30 分前 100） (50%) 0xaaa1",
            "$NEAR 峰 $180K → $250K 段 (72%) 0xaaa1",
            "$NEAR 峰 1.5x → 3x (50%) 0xaaa1",
            "$NEAR 24h 出来高 $520K → $700K 段 (74%) 0xaaa1",
        ):
            self.assertIn(expect, text)

    def test_fired_trigger_excluded(self):
        self.add("0xbbb222", "FIRED", [
            (35, {"mcap": 150_000, "holders": 1_000, "buys5m": 250, "sells5m": 10}),
            (2, {"mcap": 150_000, "holders": 1_100, "buys5m": 280, "sells5m": 10}),
        ], fired_trigger="S3")
        text = watch.near_report(self.conn, self.cfg, 6, now=self.now)
        self.assertNotIn("買 280", text)      # S3 は発火済みなので出さない
        self.assertIn("$FIRED +10%/30 分", text)  # S4 は未発火なので出す

    def test_consumed_ladder_step_moves_to_next(self):
        self.add("0xccc333", "STEP", [
            (35, {"mcap": 300_000}), (2, {"mcap": 400_000})])
        self.conn.execute(
            'INSERT INTO events(ts,chain,address,"trigger",stage,value,heat,yabai,message,'
            "notified,sent) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (self.now - 3600, "bsc", "0xccc333", "M1", "250000", 250000.0, 1, 0, "", 0, 0))
        self.conn.commit()
        text = watch.near_report(self.conn, self.cfg, 6, now=self.now)
        self.assertIn("$STEP 峰 $400K → $500K 段 (80%) 0xccc3", text)

    def test_empty_db(self):
        text = watch.near_report(self.conn, self.cfg, 6, now=self.now)
        self.assertIn("近かった銘柄", text)
        self.assertIn("[BSC]", text)

    def test_too_small_token_is_not_listed(self):
        # 規模の下限を通らない銘柄は判定していないので「近い」にも数えない
        self.add("0xddd444", "TINY", [
            (35, {"mcap": 40_000, "holders": 1_000, "buys5m": 250, "sells5m": 10,
                  "vol5m": 3_000, "vol24h": 50_000}),
            (2, {"mcap": 45_000, "holders": 1_200, "buys5m": 280, "sells5m": 10,
                 "vol5m": 4_000, "vol24h": 60_000}),
        ])
        self.assertNotIn("$TINY", watch.near_report(self.conn, self.cfg, 6, now=self.now))


class TestProcessRow(unittest.TestCase):
    """1 銘柄 1 サイクルにつき通知は 1 通・本文を持つ events 行も 1 行だけ。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.connect(os.path.join(self.tmp, "state.db"))
        self.now = int(time.time())
        self.cfg = cfg()
        self.sent = []

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sender(self, message):
        self.sent.append(message)
        return True

    def seed(self, addr, snaps, creation_ts=None, first_price=1.0):
        self.conn.execute(
            "INSERT INTO tokens(chain,address,symbol,creation_ts,first_seen_ts,first_price,"
            "first_mcap) VALUES(?,?,?,?,?,?,?)",
            ("bsc", addr, "TEST", creation_ts or (self.now - 7200), self.now - 7200,
             first_price, 200_000))
        for mins, s in snaps:
            db.insert_snapshot(self.conn, "bsc", addr, self.now - mins * 60, s)
        self.conn.commit()

    def rows(self, addr):
        return [dict(r) for r in self.conn.execute(
            'SELECT "trigger",stage,message,notified,sent FROM events WHERE address=?'
            ' ORDER BY "trigger"', (addr,))]

    def test_multiple_triggers_send_once(self):
        self.seed("0xdd", [(31, {"holders": 200, "mcap": 200_000, "price": 1.0}),
                           (2, {"holders": 210, "mcap": 200_000, "price": 1.0})])
        row = {"chain": "bsc", "address": "0xdd", "symbol": "TEST", "price": 1.0,
               "mcap": 300_000, "liquidity": 50_000, "buys5m": 320, "sells5m": 150,
               "holders": 260, "smart": 0, "visiting": 10,
               "pc5m": 3.0, "pc1h": 20.0, "vol5m": 3_000.0}
        message, fired, ok, pending = watch.process_row(
            self.conn, self.cfg, row, self.now, self.sender)
        self.conn.commit()
        self.assertEqual(len(self.sent), 1)          # Telegram へは 1 通だけ
        self.assertEqual(fired, 3)                   # S3 + S4 + M1
        self.assertEqual(pending, 0)                 # 時価総額 $300K なので即時
        self.assertTrue(ok)
        rows = self.rows("0xdd")
        self.assertEqual([r["trigger"] for r in rows], ["M1", "S3", "S4"])
        self.assertEqual([r["notified"] for r in rows], [1, 1, 1])
        self.assertEqual(sum(1 for r in rows if r["message"]), 1)
        self.assertEqual(sum(r["sent"] for r in rows), 1)
        self.assertIn("M1:", message)
        self.assertIn("S3:", message)

    def test_ladder_marker_is_not_a_notification(self):
        # 初認識（前回スナップショットなし）では M1 は段を消費するだけで通知しない
        self.seed("0xee", [])
        row = {"chain": "bsc", "address": "0xee", "symbol": "TEST", "price": 1.0,
               "mcap": 300_000, "liquidity": 50_000, "buys5m": 5, "sells5m": 5,
               "holders": 500, "visiting": 10, "pc5m": 1.0, "pc1h": 30.0,
               "vol5m": 3_000.0}
        message, fired, ok, pending = watch.process_row(
            self.conn, self.cfg, row, self.now, self.sender)
        self.conn.commit()
        self.assertEqual((message, fired, ok, pending), ("", 0, False, 0))
        self.assertEqual(self.sent, [])
        rows = self.rows("0xee")
        self.assertEqual([(r["trigger"], r["stage"], r["notified"]) for r in rows],
                         [("M1", "250000", 0)])
        self.assertEqual(db.trigger_counts(self.conn, self.now - 60), {})

    def test_filtered_row_writes_nothing(self):
        row = {"chain": "bsc", "address": "0xff", "symbol": "TEST", "price": 1.0,
               "mcap": 300_000, "liquidity": 500, "buys5m": 900, "sells5m": 1,
               "holders": 500, "visiting": 10, "pc1h": 90.0}
        self.assertEqual(watch.process_row(self.conn, self.cfg, row, self.now, self.sender),
                         ("", 0, False, 0))
        self.assertEqual(self.rows("0xff"), [])

    def test_snapshot_gap_and_active_tracking(self):
        row = {"chain": "bsc", "address": "0x11", "symbol": "TEST", "price": 1.0,
               "mcap": 200_000, "liquidity": 50_000, "buys5m": 5, "sells5m": 5,
               "holders": 500, "visiting": 10, "pc5m": 0.0, "pc1h": 0.0, "vol5m": 100.0}
        watch.process_row(self.conn, self.cfg, row, self.now, None)
        watch.process_row(self.conn, self.cfg, row, self.now + 30, None)   # 90 秒以内
        watch.process_row(self.conn, self.cfg, row, self.now + 120, None)  # 90 秒以上
        self.conn.commit()
        ts = [r["ts"] for r in self.conn.execute(
            "SELECT ts FROM snapshots WHERE address='0x11' ORDER BY ts")]
        self.assertEqual(ts, [self.now, self.now + 120])
        # 取引のない銘柄は last_active_ts を伸ばさない＝一括更新の対象から落ちていく
        quiet = dict(row, address="0x22", buys5m=0, sells5m=0, vol5m=0.0)
        watch.process_row(self.conn, self.cfg, quiet, self.now, None)
        watch.process_row(self.conn, self.cfg, quiet, self.now + 3600, None)
        self.conn.commit()
        active = db.active_addresses(self.conn, self.now + 60, ["bsc"])
        self.assertIn(("bsc", "0x11"), active)
        self.assertNotIn(("bsc", "0x22"), active)

    def test_counts_split_messages_and_fires(self):
        self.seed("0xdd", [(31, {"holders": 200, "mcap": 200_000, "price": 1.0}),
                           (2, {"holders": 210, "mcap": 200_000, "price": 1.0})])
        row = {"chain": "bsc", "address": "0xdd", "symbol": "TEST", "price": 1.0,
               "mcap": 300_000, "liquidity": 50_000, "buys5m": 320, "sells5m": 150,
               "holders": 260, "smart": 0, "visiting": 10,
               "pc5m": 3.0, "pc1h": 20.0, "vol5m": 3_000.0}
        watch.process_row(self.conn, self.cfg, row, self.now, self.sender)
        self.conn.commit()
        since = self.now - 60
        self.assertEqual(db.message_count(self.conn, since), 1)
        self.assertEqual(db.trigger_counts(self.conn, since), {"M1": 1, "S3": 1, "S4": 1})


class TestBatchDelivery(unittest.TestCase):
    """2 段配信: やばい・大きい銘柄だけ即時、残りは 5 分ごとに 1 通へまとめる。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.connect(os.path.join(self.tmp, "state.db"))
        self.now = int(time.time())
        self.cfg = cfg()
        self.sent = []

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sender(self, message):
        self.sent.append(message)
        return True

    def seed(self, addr, symbol="TEST", first_price=1.0):
        self.conn.execute(
            "INSERT INTO tokens(chain,address,symbol,creation_ts,first_seen_ts,first_price,"
            "first_mcap) VALUES(?,?,?,?,?,?,?)",
            ("bsc", addr, symbol, self.now - 7200, self.now - 7200, first_price, 150_000))
        self.conn.commit()

    def rush(self, addr, mcap=150_000, **kw):
        """S3（殺到）で発火する行。既定は「規模の下限は通るが即時ではない」大きさ。"""
        r = {"chain": "bsc", "address": addr, "symbol": "TEST", "price": 1.0,
             "mcap": mcap, "liquidity": 50_000, "buys5m": 320, "sells5m": 150,
             "holders": 500, "visiting": 10, "pc5m": 3.0, "pc1h": 20.0,
             "vol5m": 3_000.0, "vol24h": 400_000.0}
        r.update(kw)
        return r

    def chain_row(self, addr, mcap=150_000, **kw):
        """高速レーン（chain/agg.py の row()）が出す形: 保有者・スマート・注目・
        launchpad は API 側でしか埋まらないので None、logo キー自体が無い。"""
        r = {"chain": "bsc", "address": addr, "symbol": "TEST", "source": "chain",
             "price": 1.0, "mcap": mcap, "mcap_is_fdv": True, "liquidity": 50_000,
             "buys5m": 320, "sells5m": 150, "buyers5m": 160, "vol5m": 3_000.0,
             "vol24h": 400_000.0, "pc5m": 3.0, "pc1h": 20.0, "pc24h": None,
             "holders": None, "smart": None, "renowned": None, "visiting": None,
             "launchpad": None, "wash": None, "honeypot": None, "created_ts": None}
        r.update(kw)
        return r

    def events(self, addr):
        return [dict(r) for r in self.conn.execute(
            'SELECT "trigger",text,message,notified,pending,batched_ts,sent FROM events'
            ' WHERE address=? ORDER BY "trigger"', (addr,))]

    def test_small_token_goes_to_batch(self):
        self.seed("0xb1")
        message, fired, ok, pending = watch.process_row(
            self.conn, self.cfg, self.rush("0xb1"), self.now, self.sender)
        self.conn.commit()
        self.assertEqual((message, fired, ok, pending), ("", 1, False, 1))
        self.assertEqual(self.sent, [])
        rows = self.events("0xb1")
        self.assertEqual([(r["trigger"], r["pending"], r["message"]) for r in rows],
                         [("S3", 1, "")])
        self.assertTrue(rows[0]["text"])          # まとめの 1 行に使う

    def test_big_market_cap_is_immediate(self):
        self.seed("0xb2")
        message, fired, ok, pending = watch.process_row(
            self.conn, self.cfg, self.rush("0xb2", mcap=350_000), self.now, self.sender)
        self.conn.commit()
        self.assertEqual((fired, ok, pending), (1, True, 0))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("S3:", message)
        # M1 の段消費マーク（notify=False）も含め、まとめ待ちは 1 件も残らない
        self.assertEqual({r["pending"] for r in self.events("0xb2")}, {0})

    def test_big_volume_is_immediate(self):
        self.seed("0xb3")
        _, _, ok, pending = watch.process_row(
            self.conn, self.cfg, self.rush("0xb3", vol24h=800_000.0), self.now, self.sender)
        self.assertEqual((ok, pending), (True, 0))

    def test_yabai_is_immediate_even_if_small(self):
        # 10x 到達（やばい）は時価総額 $150K でも即時
        self.seed("0xb4")
        db.insert_snapshot(self.conn, "bsc", "0xb4", self.now - 120,
                           {"price": 2.0, "mcap": 150_000})
        self.conn.commit()
        _, fired, ok, pending = watch.process_row(
            self.conn, self.cfg, self.rush("0xb4", price=10.0), self.now, self.sender)
        self.assertEqual(pending, 0)
        self.assertTrue(ok)
        self.assertIn("【やばい】", self.sent[0])

    def test_second_notification_within_cooldown_goes_to_batch(self):
        # 同じ銘柄がラダーを駆け上がっても、10 分以内の 2 通目はまとめに回す
        self.seed("0xb9")
        _, _, ok, pending = watch.process_row(
            self.conn, self.cfg, self.rush("0xb9", mcap=350_000), self.now, self.sender)
        self.assertEqual((ok, pending), (True, 0))
        self.conn.commit()
        row = self.rush("0xb9", mcap=350_000, pc1h=95.0, buys5m=40, sells5m=10)
        _, _, ok2, pending2 = watch.process_row(
            self.conn, self.cfg, row, self.now + 300, self.sender)   # 5 分後
        self.assertEqual((ok2, pending2), (False, 1))
        self.assertEqual(len(self.sent), 1)
        self.conn.commit()
        # 10 分を過ぎればまた即時に戻る（S1・S3 はクールダウン中なので S2 で撃つ）
        _, _, ok3, pending3 = watch.process_row(
            self.conn, self.cfg, self.rush("0xb9", mcap=350_000, vol5m=61_000.0, buys5m=40),
            self.now + 1200, self.sender)
        self.assertEqual((ok3, pending3), (True, 0))

    def test_yabai_ignores_the_immediate_cooldown(self):
        # やばい（10x 到達）はクールダウン中でも即時
        self.seed("0xba")
        watch.process_row(self.conn, self.cfg, self.rush("0xba", mcap=350_000),
                          self.now, self.sender)
        self.conn.commit()
        db.insert_snapshot(self.conn, "bsc", "0xba", self.now + 60,
                           {"price": 2.0, "mcap": 350_000})
        self.conn.commit()
        _, _, ok, pending = watch.process_row(
            self.conn, self.cfg, self.rush("0xba", mcap=350_000, price=10.0, buys5m=40),
            self.now + 120, self.sender)
        self.assertEqual((ok, pending), (True, 0))
        self.assertIn("【やばい】", self.sent[-1])

    def test_batch_minutes_zero_sends_everything(self):
        self.cfg["notify"]["batch_minutes"] = 0
        self.seed("0xb5")
        _, _, ok, pending = watch.process_row(
            self.conn, self.cfg, self.rush("0xb5"), self.now, self.sender)
        self.assertEqual((ok, pending), (True, 0))

    def test_batch_minutes_zero_sends_chain_lane_row_even_in_cooldown(self):
        """RPC 側で条件を満たしたら API のデータ（保有者・スマート・注目・logo）が無くても、
        やばくも大きくもなく・即時クールダウン中でも batch_minutes=0 なら待たずに送る。"""
        self.cfg["notify"]["batch_minutes"] = 0
        self.seed("0xd1")
        # mcap $150K・vol24h $400K は notify.immediate の閾値（$300K/$700K）未満＝
        # 通常なら即時にならない規模。それでも batch_minutes=0 なら即時になる。
        _, _, ok1, pending1 = watch.process_row(
            self.conn, self.cfg, self.chain_row("0xd1"), self.now, self.sender)
        self.assertEqual((ok1, pending1), (True, 0))
        self.conn.commit()
        # 5 分後（immediate_cooldown_min=10 分以内）でもまとめに回さず即時のまま
        row2 = self.chain_row("0xd1", pc1h=95.0, buys5m=40, sells5m=10, buyers5m=30)
        _, _, ok2, pending2 = watch.process_row(
            self.conn, self.cfg, row2, self.now + 300, self.sender)
        self.assertEqual((ok2, pending2), (True, 0))
        self.assertEqual(len(self.sent), 2)
        for message in self.sent:
            self.assertNotIn("None", message)

    def test_flush_waits_for_the_window(self):
        self.seed("0xb6")
        watch.process_row(self.conn, self.cfg, self.rush("0xb6"), self.now, self.sender)
        self.conn.commit()
        self.assertEqual(watch.flush_batch(self.conn, self.cfg, self.now + 60, self.sender),
                         (0, 0))
        self.assertEqual(self.sent, [])
        self.assertEqual(watch.flush_batch(self.conn, self.cfg, self.now + 301, self.sender),
                         (1, 1))
        self.conn.commit()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("$TEST S3:", self.sent[0])
        self.assertIn("時価総額 $150K", self.sent[0])
        self.assertIn("gmgn.ai/bsc/token/0xb6", self.sent[0])
        row = self.events("0xb6")[0]
        self.assertEqual((row["pending"], row["sent"]), (0, 1))
        self.assertEqual(row["batched_ts"], self.now + 301)

    def test_flush_when_pending_piles_up(self):
        for i in range(15):
            addr = f"0xc{i:02d}"
            self.seed(addr)
            watch.process_row(self.conn, self.cfg, self.rush(addr), self.now, self.sender)
        self.conn.commit()
        # 15 件たまったので 5 分を待たずに送る
        self.assertEqual(watch.flush_batch(self.conn, self.cfg, self.now + 30, self.sender),
                         (1, 15))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0].count("\n"), 15)   # 見出し + 15 行

    def test_same_token_collapses_into_one_line(self):
        self.seed("0xb7")
        watch.process_row(self.conn, self.cfg, self.rush("0xb7"), self.now, self.sender)
        # 30 分前より保有者が増えた状態でもう一度（S4 が足される）
        db.insert_snapshot(self.conn, "bsc", "0xb7", self.now - 1800,
                           {"holders": 500, "mcap": 150_000, "price": 1.0})
        watch.process_row(self.conn, self.cfg,
                          self.rush("0xb7", holders=700, buys5m=10), self.now + 60, self.sender)
        self.conn.commit()
        digests, items = watch.flush_batch(self.conn, self.cfg, self.now + 400, self.sender)
        self.assertEqual((digests, items), (1, 2))
        body = self.sent[0].split("\n")
        self.assertEqual(len(body), 2)                   # 見出し + 1 銘柄
        self.assertIn("S3:", body[1])
        self.assertIn("S4:", body[1])
        self.assertIn("1 銘柄 / 2 件", body[0])

    def test_dry_run_marks_batched_without_sending(self):
        self.seed("0xb8")
        watch.process_row(self.conn, self.cfg, self.rush("0xb8"), self.now, None)
        self.conn.commit()
        self.assertEqual(watch.flush_batch(self.conn, self.cfg, self.now + 301, None), (1, 1))
        self.conn.commit()
        row = self.events("0xb8")[0]
        self.assertEqual((row["pending"], row["sent"]), (0, 0))
        self.assertEqual(row["batched_ts"], self.now + 301)
        self.assertEqual(db.batch_counts(self.conn, self.now), (1, 1))
        self.assertEqual(db.message_count(self.conn, self.now), 0)


class TestChainLaneLine(unittest.TestCase):
    """/status の末尾に出す高速レーンの生存行（kv の心拍を読むだけ）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.connect(os.path.join(self.tmp, "state.db"))
        self.now = int(time.time())

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def beat(self, ts, block=122528493, lag=0.8):
        db.kv_set(self.conn, "chain_bsc_status",
                  json.dumps({"block": block, "lag_s": lag, "ts": ts}))
        self.conn.commit()

    def test_reports_alive_stale_and_missing(self):
        self.assertEqual(watch.chain_lane_line(self.conn, now=self.now),
                         "chain bsc: 停止中（記録なし）")
        self.beat(self.now - 12)
        self.assertEqual(watch.chain_lane_line(self.conn, now=self.now),
                         "chain bsc: block 122528493 / lag 0.8s / 12 秒前")
        self.beat(self.now - 400)
        self.assertEqual(watch.chain_lane_line(self.conn, now=self.now),
                         "chain bsc: 停止中（最終 6 分前）")
        self.assertIn("chain bsc: 停止中", watch.status_text(self.conn, cfg()))


class TestDsBudget(unittest.TestCase):
    """DexScreener は 300 コール/分。60 秒の移動窓で残り枠を数えてから撃つ。

    銘柄が 3,000 を超えて 1 サイクル 117 コール × 3 サイクル/分になり、429 の波が
    出ていた（2026-09-18 実測）。入りきらないぶんは次のサイクルに回す。
    """

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.cfg = {"chains": ["bsc"], "ds": {"enabled": True, "active_hours": 24}}
        self.now = int(time.time())

    def token(self, address, active_ts, seen_ts=None):
        self.conn.execute(
            "INSERT INTO tokens(chain,address,first_seen_ts,last_seen_ts,last_active_ts)"
            " VALUES('bsc',?,?,?,?)",
            (address, active_ts, active_ts if seen_ts is None else seen_ts, active_ts))

    def many(self, n):
        for i in range(n):
            self.token(f"0x{i:04d}", self.now - i)
        self.conn.commit()

    def test_everything_is_fetched_when_the_budget_allows(self):
        self.many(5)
        targets, skipped = watch.ds_targets(self.conn, self.cfg, ["bsc"], {}, 100)
        self.assertEqual(len(targets), 5)
        self.assertEqual(skipped, 0)

    def test_the_least_active_are_skipped(self):
        self.many(10)
        targets, skipped = watch.ds_targets(self.conn, self.cfg, ["bsc"], {}, 4)
        self.assertEqual((len(targets), skipped), (4, 6))
        self.assertIn(("bsc", "0x0000"), targets)      # 直近に動いた銘柄は残す
        self.assertNotIn(("bsc", "0x0008"), targets)   # 動いていない銘柄は次サイクルへ

    def test_the_stale_share_rescues_untouched_tokens(self):
        self.many(10)
        # 活動順では最下位だが、最終更新がいちばん古いので枠の一部で拾われる
        self.token("0xold", self.now - 3600, seen_ts=self.now - 7200)
        self.conn.commit()
        targets, skipped = watch.ds_targets(self.conn, self.cfg, ["bsc"], {}, 5)
        self.assertIn(("bsc", "0xold"), targets)
        self.assertEqual(skipped, 6)

    def test_new_tokens_of_this_cycle_are_always_included(self):
        self.many(10)
        bucket = {("bsc", "0xnew"): {}, ("sol", "0xelse"): {}}
        targets, _ = watch.ds_targets(self.conn, self.cfg, ["bsc"], bucket, 3)
        self.assertEqual(targets[0], ("bsc", "0xnew"))
        self.assertNotIn(("sol", "0xelse"), targets)   # 監視していないチェーンは引かない

    def test_a_used_up_budget_skips_the_whole_cycle(self):
        self.many(400)
        seen = []

        def fake(by_chain, workers=6):
            n = sum(len(v) for v in by_chain.values())
            seen.append(n)
            return [], -(-n // watch.dexscreener.MAX_ADDRS), False

        with mock.patch.object(watch.dexscreener, "fetch_many", fake):
            watch.refresh(self.conn, self.cfg, ["bsc"], {})
            db.record_api(self.conn, self.now, "ds tokens x270", True, 0, 270)
            calls, limited, skipped = watch.refresh(self.conn, self.cfg, ["bsc"], {})
        self.assertEqual(seen, [400, 0])
        self.assertEqual((calls, limited, skipped), (0, False, 400))

    def test_the_budget_is_paced_by_elapsed_time(self):
        # 一度も撃っていなければ 1 分ぶん撃てる
        self.assertEqual(watch.ds_budget(self.conn, 270, self.now), 270)
        db.record_api(self.conn, self.now - 20, "ds tokens x90", True, 0, 90)
        # 窓には 180 空いているが、20 秒しか経っていないので 1 分ぶんの 1/3 まで
        self.assertEqual(watch.ds_budget(self.conn, 270, self.now), 90)

    def test_a_long_gap_is_still_capped_by_the_60_second_window(self):
        db.record_api(self.conn, self.now, "ds tokens x260", True, 0, 260)
        # 50 秒あいても、直近 60 秒で 260 撃っていれば残りは 10
        self.assertEqual(watch.ds_budget(self.conn, 270, self.now + 50), 10)

    def test_the_recorded_count_is_calls_not_rows(self):
        self.many(100)
        with mock.patch.object(watch.dexscreener, "fetch_many",
                               lambda by_chain, workers=6: ([], 4, False)):
            watch.refresh(self.conn, self.cfg, ["bsc"], {})
        # 1 行にまとめて記録しても、枠は「4 コール」として数える
        self.assertEqual(db.api_calls_since(self.conn, self.now - 60, "ds "), 4)


class TestSendPhotoTimeout(unittest.TestCase):
    """sendPhoto は 10 秒以内の送信を優先して 6 秒で打ち切る（テキスト送信は 30 秒のまま）。"""

    def fake_api(self, token, method, params, timeout=30, files=None):
        self.calls.append((method, timeout))
        return {"message_id": 1}, None

    def setUp(self):
        self.calls = []

    def test_send_photo_uses_timeout_6(self):
        with mock.patch.object(notify, "api", self.fake_api):
            ok = notify.send_photo("https://example.com/x.png", "cap", "TOKEN", "CHAT")
        self.assertTrue(ok)
        self.assertEqual(self.calls, [("sendPhoto", 6)])

    def test_send_text_keeps_timeout_30(self):
        with mock.patch.object(notify, "api", self.fake_api):
            notify.send("hello", "TOKEN", "CHAT")
        self.assertEqual(self.calls, [("sendMessage", 30)])


class TestMultipart(unittest.TestCase):
    """画像本体は multipart/form-data で送る（URL を Telegram に取りに行かせない）。"""

    def test_the_body_has_the_boundary_and_the_parts(self):
        body, ctype = notify.multipart({"chat_id": "1", "caption": "本文"},
                                       {"photo": ("x.png", b"\x89PNG", "image/png")})
        boundary = ctype.split("boundary=")[1]
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        text = body.decode("utf-8", "replace")
        self.assertIn(f"--{boundary}\r\n", text)
        self.assertIn('Content-Disposition: form-data; name="chat_id"\r\n\r\n1\r\n', text)
        self.assertIn('Content-Disposition: form-data; name="caption"\r\n\r\n本文\r\n', text)
        self.assertIn('Content-Disposition: form-data; name="photo"; filename="x.png"\r\n'
                      "Content-Type: image/png\r\n\r\n", text)
        self.assertIn(b"\x89PNG", body)
        self.assertTrue(text.endswith(f"--{boundary}--\r\n"))

    def test_each_call_gets_its_own_boundary(self):
        first = notify.multipart({}, {"photo": ("a", b"1", "image/png")})[1]
        second = notify.multipart({}, {"photo": ("a", b"1", "image/png")})[1]
        self.assertNotEqual(first, second)


class TestSendPhotoSource(unittest.TestCase):
    """手元のファイルなら multipart、URL ならこれまでどおり URL を渡す。"""

    def setUp(self):
        self.calls = []

    def api(self):
        def fake(token, method, params, timeout=30, files=None):
            self.calls.append((method, params, files))
            return {"message_id": 3}, None
        return mock.patch.object(notify, "api", fake)

    def test_a_local_file_is_sent_as_multipart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "logo.webp")
            with open(path, "wb") as f:
                f.write(b"RIFF")
            with self.api():
                self.assertIsNotNone(notify.send_photo(path, "cap", "T", "C"))
        _method, params, files = self.calls[0]
        self.assertNotIn("photo", params)
        self.assertEqual(files["photo"], ("logo.webp", b"RIFF", "image/webp"))

    def test_a_url_is_passed_through(self):
        with self.api():
            notify.send_photo("https://img/x.png", "cap", "T", "C")
        _method, params, files = self.calls[0]
        self.assertIsNone(files)
        self.assertEqual(params["photo"], "https://img/x.png")

    def test_a_missing_file_is_not_treated_as_a_path(self):
        self.assertIsNone(notify.photo_file("/no/such/logo.png"))

    def test_send_returns_the_telegram_result(self):
        with self.api():
            res = notify.send("hello", "T", "C")
        self.assertEqual(res["message_id"], 3)
        self.assertTrue(notify.sent_ok(res))
        self.assertFalse(notify.sent_ok(None))
        self.assertFalse(notify.sent_ok({"ok": False}))
        self.assertTrue(notify.sent_ok({"ok": True}))


class TestTimelineText(unittest.TestCase):
    """/timeline はその銘柄に送った通知の時系列を新しい順に出す。"""

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.now = 1_000_000

    def alert(self, **kw):
        row = {"chain": "bsc", "address": "0xtok", "symbol": "SYM", "lane": "chain",
               "level": "HOT", "score": 72.0, "created_ts": self.now - 600,
               "first_seen_ts": self.now - 565, "rise_start_ts": self.now - 400,
               "reach_ts": self.now - 2, "judged_ts": self.now,
               "send_start_ts": self.now + 0.1, "sent_ts": self.now + 0.4,
               "photo": 1, "message_id": 812, "ok": 1}
        row.update(kw)
        db.insert_alert(self.conn, row)
        self.conn.commit()

    def test_it_shows_the_stages(self):
        self.alert()
        text = watch.timeline_text(self.conn, "bsc", "0xtok")
        self.assertIn("通知の記録 1 件", text)
        self.assertIn("$SYM HOT 72点（chain）", text)
        self.assertIn("初観測 +35s", text)
        self.assertIn("画像=○ id=812", text)

    def test_created_ts_falls_back_to_the_token_row(self):
        # 通知の時点では作成時刻が分からず、あとで DexScreener 等から入ったケース
        self.alert(created_ts=None)
        db.ensure_token(self.conn, "bsc", "0xtok", creation_ts=self.now - 700)
        self.conn.commit()
        text = watch.timeline_text(self.conn, "bsc", "0xtok")
        self.assertIn("作成 " + time.strftime("%H:%M:%S", time.localtime(self.now - 700)), text)
        self.assertIn("初観測 +2m15s", text)

    def test_the_newest_comes_first_and_the_limit_applies(self):
        self.alert(judged_ts=self.now, level="WATCH")
        self.alert(judged_ts=self.now + 60, level="STRONG")
        text = watch.timeline_text(self.conn, "bsc", "0xtok", limit=1)
        self.assertIn("STRONG", text)
        self.assertNotIn("WATCH", text)

    def test_the_chain_may_be_omitted(self):
        self.alert(chain="arc", address="0xarc")
        self.assertIn("$SYM", watch.timeline_text(self.conn, None, "0xarc"))

    def test_an_unknown_address(self):
        self.assertIn("記録なし", watch.timeline_text(self.conn, None, "0xnope"))

    def test_the_command_parses_the_chain_prefix(self):
        self.alert(chain="robinhood", address="0xrh")
        self.assertIn("$SYM", watch.timeline_command(self.conn, "rh:0xRH"))
        self.assertIn("記録なし", watch.timeline_command(self.conn, "bsc:0xrh"))

    def test_the_command_without_an_address(self):
        self.assertIn("使い方", watch.timeline_command(self.conn, ""))


if __name__ == "__main__":
    unittest.main()
