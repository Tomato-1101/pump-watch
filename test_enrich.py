"""通知の「画像・安全確認・共有バケット」まわり。ネットワークには一切出ない。

安全確認の期待値は 2026-09-18 に GoPlus（BSC）から実際に返ってきた FLAPGUY の
レスポンスそのもの（fixtures/goplus_flapguy.json）。フィールド名や型（数値も文字列で
返る）の思い込みを固定するのが目的なので、合成データではなく実データを置いてある。
公開用に、保有者・LP 保有者・作成者のアドレスとプール ID だけはダミーの連番に置き換えてある
（判定はアドレスの値に依存しないので期待値は変わらない）。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db       # noqa: E402
import enrich   # noqa: E402
import notify   # noqa: E402
import watch    # noqa: E402
from sources import dexscreener, fourmeme, gmgn, goplus  # noqa: E402

FLAPGUY = "0x378dac3bc8570ae961f4795aaef61f17ae4a7777"


def fixture():
    with open(os.path.join(HERE, "fixtures", "goplus_flapguy.json")) as f:
        return json.load(f)


def sec():
    return fixture()["result"][FLAPGUY]


def no_net(case):
    """画像本体のダウンロードと DexScreener の名指し取得を止める（既定は「取れない」）。

    enrich.logo(fetch=True) は URL を見つけたら本体も落としに行くので、
    止めないとテストが外に出る。個別に見たいテストは自分で mock し直す。
    """
    for target, name, value in ((enrich, "fetch_logo_file", None),
                                (dexscreener, "fetch", ([], 200))):
        patcher = mock.patch.object(target, name, return_value=value)
        patcher.start()
        case.addCleanup(patcher.stop)


class TestSecurityLine(unittest.TestCase):
    def test_parts_from_real_response(self):
        parts = enrich.security_parts(sec())
        # 売り税 0.0099 は「1%」に丸める。owner_address がゼロなので「放棄」。
        # LP は焼却もロックもされていない（is_locked=0・宛先が dead でない）ので 0%。
        self.assertEqual(parts, ["honeypot なし", "税 買0%/売1%", "オーナー放棄",
                                 "LP 焼却/ロック 0%"])

    def test_no_danger_line_for_a_clean_token(self):
        self.assertEqual(enrich.danger_parts(sec()), [])

    def test_holder_count(self):
        self.assertEqual(enrich.holder_count(sec()), 657)

    def test_missing_security_is_shown_as_such(self):
        self.assertEqual(enrich.security_parts(None), ["未取得"])
        self.assertEqual(enrich.danger_parts(None), [])

    def test_burned_lp_counts_as_locked(self):
        s = sec()
        s["lp_holders"] = [{"address": "0x000000000000000000000000000000000000dead",
                            "percent": "0.9", "is_locked": 0},
                           {"address": "0xabc", "percent": "0.1", "is_locked": 1}]
        self.assertIn("LP 焼却/ロック 100%", enrich.security_parts(s))

    def test_owner_present(self):
        s = sec()
        s["owner_address"] = "0x000000000000000000000000000000000000100c"
        self.assertIn("オーナーあり", enrich.security_parts(s))

    def test_danger_flags(self):
        s = sec()
        s["is_mintable"] = "1"
        s["hidden_owner"] = "1"
        s["transfer_pausable"] = "1"
        self.assertEqual(enrich.danger_parts(s), ["ミント可", "隠れオーナー", "送金停止可"])

    def test_high_sell_tax_is_a_danger(self):
        s = sec()
        s["sell_tax"] = "0.15"
        self.assertEqual(enrich.danger_parts(s), ["売り税 15%"])
        # 表示行のほうにも出る（買 0% / 売 15%）
        self.assertIn("税 買0%/売15%", enrich.security_parts(s))

    def test_honeypot(self):
        s = sec()
        s["is_honeypot"] = "1"
        self.assertIn("honeypot あり", enrich.security_parts(s))
        self.assertIn("honeypot", enrich.danger_parts(s))


def _row(**kw):
    import rules
    row = {k: None for k in rules.ROW_KEYS}
    row.update({"chain": "bsc", "address": FLAPGUY, "symbol": "FLAPGUY", "source": "chain",
                "price": 0.00018, "mcap": 184_000, "liquidity": 52_700,
                "buys5m": 59, "sells5m": 97, "buyers5m": 21, "vol5m": 39_900,
                "pc1h": 131.0, "pc24h": 131.0, "created_ts": 1_000_000 - 16 * 60})
    row.update(kw)
    return row


class TestBuildMessage(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads(json.dumps(watch.DEFAULTS))
        self.now = 1_000_000
        self.events = [watch.rules.Event(
            "S2", stage="x", text="出来高急増 5 分 $39.9K（平常比 20.8x）", heat=4, yabai=True)]

    def build(self, security, **kw):
        return watch.build_message(_row(**kw), {}, self.events, self.now, self.cfg, security)

    def test_safety_line(self):
        msg = self.build(sec())
        self.assertIn("安全: honeypot なし ・ 税 買0%/売1% ・ オーナー放棄"
                      " ・ LP 焼却/ロック 0% ・ 保有者 657", msg)
        self.assertNotIn("注意:", msg)          # 危険フラグが無ければ行ごと出ない

    def test_danger_line_appears_only_when_flagged(self):
        s = sec()
        s["is_mintable"] = "1"
        self.assertIn("注意: ミント可", self.build(s))

    def test_missing_security(self):
        msg = self.build(None)
        self.assertIn("安全: 未取得", msg)
        self.assertNotIn("注意:", msg)

    def test_gmgn_values_join_the_same_line(self):
        """GoPlus 非対応チェーンでも GMGN 側の値だけで「安全:」行を組む。"""
        msg = self.build(None, top10_rate=0.22, rug_ratio=0.0, sniper_count=3, holders=657)
        self.assertIn("安全: 未取得 ・ top10 22% ・ sniper 3 ・ 保有者 657", msg)

    def test_head_and_footer(self):
        msg = self.build(sec(), launchpad="four.meme", launchpad_progress=0.63)
        lines = msg.split("\n")
        self.assertEqual(lines[0], "<b>【やばい】$FLAPGUY</b>  BSC · four.meme 進捗 63%"
                                   " · 作成 16 分前")
        self.assertEqual(lines[1], "S2: 出来高急増 5 分 $39.9K（平常比 20.8x）")
        self.assertIn("熱度 4/10 ・ 源 chain", msg)
        self.assertTrue(msg.endswith(f"<code>{FLAPGUY}</code>"))

    def test_links(self):
        links = watch.alert_links(_row(launchpad="four.meme"))
        self.assertEqual([t for t, _ in links], ["GMGN", "DexScreener", "four.meme"])
        self.assertEqual(links[0][1], f"https://gmgn.ai/bsc/token/{FLAPGUY}")
        self.assertEqual(links[1][1], f"https://dexscreener.com/bsc/{FLAPGUY}")
        # launchpad が無い / flap のときは公式リンクを付けない（URL 形式が未確認）
        self.assertEqual(len(watch.alert_links(_row(launchpad="flap"))), 2)


class TestCaption(unittest.TestCase):
    def test_long_caption_is_trimmed_to_the_limit(self):
        text = "\n".join(f"<b>{i:03d}</b> " + "あ" * 40 for i in range(80))
        out = notify.trim_caption(text)
        self.assertGreater(len(text), notify.CAPTION_LEN)
        self.assertLessEqual(len(out), notify.CAPTION_LEN)
        self.assertTrue(out.endswith("\n…"))
        # 行ごと落とすので、タグが途中で切れていない
        self.assertEqual(out.count("<b>"), out.count("</b>"))

    def test_short_caption_is_untouched(self):
        self.assertEqual(notify.trim_caption("短い本文"), "短い本文")


class TestSendAlert(unittest.TestCase):
    """画像 → 失敗したらテキスト、の落ち方（Telegram API はモック）。"""

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.calls = []
        self.cfg = json.loads(json.dumps(watch.DEFAULTS))
        db.upsert_token(self.conn, "bsc", FLAPGUY, {"symbol": "FLAPGUY"}, 1_000_000)
        db.save_gmgn_info(self.conn, "bsc", FLAPGUY, "https://img/logo.png", None, 1_000_000)
        self.conn.commit()
        no_net(self)

    def tearDown(self):
        self.conn.close()

    def fake_api(self, photo_ok):
        def api(token, method, params, timeout=30, files=None):
            self.calls.append((method, params, files))
            if method == "sendPhoto" and not photo_ok:
                return None, "failed to get HTTP URL content"
            return {"message_id": 1}, None
        return api

    def test_photo_is_used_when_the_logo_is_cached(self):
        with mock.patch.object(notify, "api", self.fake_api(True)):
            ok = watch.send_alert(self.conn, self.cfg, _row(), "本文", ("t", "1"))
        self.assertTrue(ok)
        self.assertEqual([m for m, _p, _f in self.calls], ["sendPhoto"])
        params = self.calls[0][1]
        self.assertEqual(params["photo"], "https://img/logo.png")
        self.assertEqual(params["caption"], "本文")
        # リンクは本文の生 URL。ボタンはアプリが「開きますか」と聞くので付けない
        self.assertNotIn("reply_markup", params)

    def test_falls_back_to_text_when_sendphoto_fails(self):
        with mock.patch.object(notify, "api", self.fake_api(False)):
            ok = watch.send_alert(self.conn, self.cfg, _row(), "本文", ("t", "1"))
        self.assertTrue(ok)
        self.assertEqual([m for m, _p, _f in self.calls], ["sendPhoto", "sendMessage"])
        self.assertEqual(self.calls[1][1]["text"], "本文")   # 本文はそのまま（リンクは本文側にある）
        self.assertNotIn("reply_markup", self.calls[1][1])

    def test_long_body_skips_the_photo(self):
        # caption は 1024 字まで。切り詰めるとリンクや名簿の行が消えるので画像なしで送る
        body = "x" * (notify.CAPTION_LEN + 1)
        with mock.patch.object(notify, "api", self.fake_api(True)):
            ok = watch.send_alert(self.conn, self.cfg, _row(), body, ("t", "1"))
        self.assertTrue(ok)
        self.assertEqual([m for m, _p, _f in self.calls], ["sendMessage"])

    def test_text_only_when_there_is_no_logo(self):
        db.upsert_token(self.conn, "bsc", "0xnologo", {"symbol": "X"}, 1_000_000)
        self.conn.commit()
        with mock.patch.object(notify, "api", self.fake_api(True)), \
                mock.patch.object(fourmeme, "image", return_value=None) as fm:
            ok = watch.send_alert(self.conn, self.cfg, _row(address="0xnologo"),
                                  "本文", ("t", "1"))
        self.assertTrue(ok)
        self.assertEqual([m for m, _p, _f in self.calls], ["sendMessage"])
        # 送信時は取りに行かない（先回り＝chain/run.py の Prefetch の担当）。
        # キャッシュが無ければ待たずにテキストで送る。
        fm.assert_not_called()


class TestPhotoUrl(unittest.TestCase):
    """Telegram が取りに行けない URL を sendPhoto に渡さない（2026-09-18 実測の 403 対策）。"""

    def test_gmgn_images_are_rejected(self):
        self.assertIsNone(enrich.photo_url(
            "https://gmgn.ai/external-res/fe8f905850739c896e1b8a4ed5b0240f_v2.webp"))

    def test_other_hosts_pass(self):
        url = "https://cdn.dexscreener.com/cms/images/Ah1s1MBKlL_c62Ft?width=800"
        self.assertEqual(enrich.photo_url(url), url)

    def test_empty(self):
        self.assertIsNone(enrich.photo_url(None))
        self.assertIsNone(enrich.photo_url(""))

    def test_cached_gmgn_logo_is_not_sent_as_a_photo(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        db.upsert_token(conn, "bsc", FLAPGUY, {"symbol": "F"}, 1_000_000)
        db.save_gmgn_info(conn, "bsc", FLAPGUY, "https://gmgn.ai/external-res/a.webp",
                          None, 1_000_000)
        # キャッシュは消さない（GMGN は試し済みの印）が、sendPhoto には渡さない
        self.assertIsNone(enrich.logo(conn, "bsc", FLAPGUY, now=1_000_000))


class TestDexscreenerImage(unittest.TestCase):
    """画像は DexScreener の一括更新の応答から拾う（追加コール 0）。"""

    def pair(self, image):
        info = {"imageUrl": image} if image else {}
        return {"baseToken": {"address": "0xABC", "symbol": "X"}, "info": info}

    def test_image_is_picked_up(self):
        row = dexscreener._row("bsc", self.pair("https://cdn.dexscreener.com/x.png"))
        self.assertEqual(row["logo"], "https://cdn.dexscreener.com/x.png")

    def test_missing_image_is_none(self):
        self.assertIsNone(dexscreener._row("bsc", self.pair(None))["logo"])

    def test_it_replaces_an_unusable_cached_logo(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        cfg = json.loads(json.dumps(watch.DEFAULTS))
        row = _row(logo="https://cdn.dexscreener.com/x.png")
        db.upsert_token(conn, "bsc", FLAPGUY, {"symbol": "FLAPGUY"}, 1_000_000)
        db.save_gmgn_info(conn, "bsc", FLAPGUY, "https://gmgn.ai/external-res/a.webp",
                          None, 1_000_000)
        watch.process_row(conn, cfg, row, 1_000_000, sender=lambda text: True)
        self.assertEqual(db.get_token(conn, "bsc", FLAPGUY)["logo"],
                         "https://cdn.dexscreener.com/x.png")

    def test_a_usable_cached_logo_is_kept(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        cfg = json.loads(json.dumps(watch.DEFAULTS))
        row = _row(logo="https://cdn.dexscreener.com/new.png")
        db.upsert_token(conn, "bsc", FLAPGUY, {"symbol": "FLAPGUY"}, 1_000_000)
        db.save_logo(conn, "bsc", FLAPGUY, "https://img/old.png")
        watch.process_row(conn, cfg, row, 1_000_000, sender=lambda text: True)
        self.assertEqual(db.get_token(conn, "bsc", FLAPGUY)["logo"], "https://img/old.png")


class TestFourmemeImage(unittest.TestCase):
    """DexScreener に画像が無い BSC 銘柄だけ、通知時に four.meme へ 1 回だけ聞く。

    期待値は 2026-09-18 に four.meme の公開 API から実際に返ってきた形
    （知らないアドレスは 200 で `data` ごと無い）。
    """

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.now = 1_000_000
        db.upsert_token(self.conn, "bsc", FLAPGUY, {"symbol": "F"}, self.now)
        no_net(self)

    def get_json(self, payload):
        return mock.patch.object(fourmeme.http, "get_json", return_value=(payload, 200))

    def test_image_is_read_from_the_payload(self):
        payload = {"code": 0, "msg": "success",
                   "data": {"image": "https://static.four.meme/market/917OZ93xhq53Y.png"}}
        with mock.patch.object(fourmeme, "MIN_GAP", 0), self.get_json(payload):
            self.assertEqual(fourmeme.image(FLAPGUY),
                             "https://static.four.meme/market/917OZ93xhq53Y.png")

    def test_unknown_address_returns_none(self):
        with mock.patch.object(fourmeme, "MIN_GAP", 0), \
                self.get_json({"code": 0, "msg": "success"}):
            self.assertIsNone(fourmeme.image(FLAPGUY))

    def test_images_from_other_hosts_are_ignored(self):
        with mock.patch.object(fourmeme, "MIN_GAP", 0), \
                self.get_json({"data": {"image": "https://example.com/x.png"}}):
            self.assertIsNone(fourmeme.image(FLAPGUY))

    def test_a_hit_is_cached(self):
        url = "https://static.four.meme/market/abc.png"
        with mock.patch.object(fourmeme, "image", return_value=url) as fm:
            self.assertEqual(enrich.logo(self.conn, "bsc", FLAPGUY, fetch=True,
                                         now=self.now), url)
            # 2 回目はキャッシュだけで返す
            self.assertEqual(enrich.logo(self.conn, "bsc", FLAPGUY, fetch=True,
                                         now=self.now), url)
        fm.assert_called_once()
        self.assertEqual(db.get_token(self.conn, "bsc", FLAPGUY)["logo"], url)

    def test_a_miss_is_cached_too(self):
        with mock.patch.object(fourmeme, "image", return_value=None) as fm:
            self.assertIsNone(enrich.logo(self.conn, "bsc", FLAPGUY, fetch=True,
                                          now=self.now))
            self.assertIsNone(enrich.logo(self.conn, "bsc", FLAPGUY, fetch=True,
                                          now=self.now))
        fm.assert_called_once()          # 画像が無い銘柄にも二度撃たない
        self.assertEqual(db.get_token(self.conn, "bsc", FLAPGUY)["logo_ts"], self.now)

    def test_other_chains_are_not_asked(self):
        db.upsert_token(self.conn, "sol", "So1", {"symbol": "S"}, self.now)
        with mock.patch.object(fourmeme, "image") as fm:
            self.assertIsNone(enrich.logo(self.conn, "sol", "So1", fetch=True, now=self.now))
        fm.assert_not_called()

    def test_nothing_is_fetched_without_fetch(self):
        with mock.patch.object(fourmeme, "image") as fm:
            self.assertIsNone(enrich.logo(self.conn, "bsc", FLAPGUY, now=self.now))
        fm.assert_not_called()


class TestSharedBucket(unittest.TestCase):
    """GMGN の枠は 2 プロセスで共有するので api_calls 表の行数で測る。"""

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.now = 1_000_000
        no_net(self)

    def tearDown(self):
        self.conn.close()

    def used(self, n, age=10):
        for i in range(n):
            db.record_api(self.conn, self.now - age, gmgn.CMD_PREFIX + f"token info {i}", True, 0)
        self.conn.commit()

    def test_window_is_210_seconds(self):
        self.used(6, age=gmgn.BUCKET_WINDOW + 1)     # 窓の外＝数えない
        self.assertEqual(gmgn.bucket_used(self.conn, self.now), 0)
        self.used(2, age=gmgn.BUCKET_WINDOW - 1)
        self.assertEqual(gmgn.bucket_used(self.conn, self.now), 2)

    def test_notify_may_use_more_than_backfill(self):
        self.used(3)
        # 巡回の補完は 3 で打ち止め、通知のための取得はまだ撃てる
        self.assertFalse(gmgn.has_room(self.conn, gmgn.BACKFILL_ROOM, self.now))
        self.assertTrue(gmgn.has_room(self.conn, gmgn.NOTIFY_ROOM, self.now))
        self.used(3)
        self.assertFalse(gmgn.has_room(self.conn, gmgn.NOTIFY_ROOM, self.now))

    def test_other_commands_do_not_count(self):
        db.record_api(self.conn, self.now, "gt /networks/bsc", True, 0)
        self.conn.commit()
        self.assertEqual(gmgn.bucket_used(self.conn, self.now), 0)

    def test_backfill_is_skipped_without_room(self):
        db.upsert_token(self.conn, "bsc", FLAPGUY, {"symbol": "F"}, self.now)
        self.used(gmgn.BACKFILL_ROOM)
        with mock.patch.object(gmgn, "token_info") as ti:
            self.assertEqual(enrich.backfill_logos(self.conn, ["bsc"], self.now), 0)
        ti.assert_not_called()

    def test_the_notify_path_no_longer_touches_the_bucket(self):
        # 通知経路は GMGN を引かない（枠を使い切っていても four.meme には聞ける）
        db.upsert_token(self.conn, "bsc", FLAPGUY, {"symbol": "F"}, self.now)
        self.used(gmgn.NOTIFY_ROOM)
        with mock.patch.object(gmgn, "token_info") as ti, \
                mock.patch.object(fourmeme, "image", return_value="https://img/x.png") as fm:
            self.assertEqual(enrich.logo(self.conn, "bsc", FLAPGUY, fetch=True,
                                         now=self.now), "https://img/x.png")
        ti.assert_not_called()
        fm.assert_called_once()


class TestSecurityCache(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.now = 1_000_000
        db.upsert_token(self.conn, "bsc", FLAPGUY, {"symbol": "FLAPGUY"}, self.now)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_fetch_then_cache(self):
        with mock.patch.object(goplus, "fetch", return_value=(sec(), "ok")) as f:
            got = enrich.security(self.conn, "bsc", FLAPGUY, self.now)
            self.assertEqual(got["token_symbol"], "FLAPGUY")
            # 30 分以内は引き直さない
            enrich.security(self.conn, "bsc", FLAPGUY, self.now + enrich.SECURITY_TTL - 1)
        f.assert_called_once()
        with mock.patch.object(goplus, "fetch", return_value=(sec(), "ok")) as f:
            enrich.security(self.conn, "bsc", FLAPGUY, self.now + enrich.SECURITY_TTL + 1)
        f.assert_called_once()

    def test_unsupported_chain_is_asked_only_once(self):
        with mock.patch.object(goplus, "fetch", return_value=(None, "unsupported")) as f:
            self.assertIsNone(enrich.security(self.conn, "arc", "0xzz", self.now))
            self.assertIsNone(enrich.security(self.conn, "arc", "0xyy", self.now))
        f.assert_called_once()
        self.assertTrue(db.kv_get(self.conn, "goplus_unsupported_arc"))

    def test_an_unknown_token_does_not_disable_the_whole_chain(self):
        """GoPlus は知らない銘柄にも空の result を返す（チェーン未対応と同じ形）。

        一度でも取れているチェーンは、その 1 件で止めない（2026-09-18: 先回り取得が
        知らない銘柄を聞いた瞬間に BSC 全体の安全確認が止まった）。
        """
        with mock.patch.object(goplus, "fetch", return_value=(sec(), "ok")):
            enrich.security(self.conn, "bsc", FLAPGUY, self.now)
        db.upsert_token(self.conn, "bsc", "0xunknown", {"symbol": "U"}, self.now)
        with mock.patch.object(goplus, "fetch", return_value=(None, "unsupported")) as f:
            self.assertIsNone(enrich.security(self.conn, "bsc", "0xunknown", self.now))
            self.assertIsNone(enrich.security(self.conn, "bsc", "0xunknown", self.now + 1))
        self.assertIsNone(db.kv_get(self.conn, "goplus_unsupported_bsc"))
        self.assertEqual(f.call_count, 2)     # 銘柄単位では聞き直す（チェーンは止めない）

    def test_transient_error_keeps_the_old_value(self):
        with mock.patch.object(goplus, "fetch", return_value=(sec(), "ok")):
            enrich.security(self.conn, "bsc", FLAPGUY, self.now)
        with mock.patch.object(goplus, "fetch", return_value=(None, "error")):
            got = enrich.security(self.conn, "bsc", FLAPGUY, self.now + 99999)
        self.assertEqual(got["token_symbol"], "FLAPGUY")

    def test_chain_ids(self):
        self.assertEqual(goplus.CHAIN_IDS["bsc"], "56")
        self.assertIsNone(goplus.fetch("unknownchain", "0x1")[0])
        self.assertEqual(goplus.fetch("unknownchain", "0x1")[1], "unsupported")


if __name__ == "__main__":
    unittest.main()


class TestSendAlertResult(unittest.TestCase):
    """send_alert の戻りは「送信の実測」。タイムラインの送信〜TG 受理の段になる。"""

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.cfg = json.loads(json.dumps(watch.DEFAULTS))
        self.calls = []
        db.upsert_token(self.conn, "bsc", FLAPGUY, {"symbol": "F"}, 1_000_000)
        db.save_logo(self.conn, "bsc", FLAPGUY, "https://img/logo.png")
        self.conn.commit()

    def api(self, ok=True):
        def fake(token, method, params, timeout=30, files=None):
            self.calls.append((method, params, files))
            if not ok:
                return None, "Bad Request: chat not found"
            return {"message_id": 812, "date": 1_700_000_000}, None
        return mock.patch.object(notify, "api", fake)

    def test_it_returns_the_send_measurements(self):
        with self.api(), mock.patch.object(enrich, "fetch_logo_file", return_value=None):
            res = watch.send_alert(self.conn, self.cfg, _row(), "本文", ("t", "1"))
        self.assertTrue(res["ok"])
        self.assertTrue(res["photo"])
        self.assertEqual((res["message_id"], res["tg_date"]), (812, 1_700_000_000))
        self.assertLessEqual(res["send_start_ts"], res["sent_ts"])
        self.assertIsNone(res["error"])

    def test_a_failure_carries_the_reason(self):
        with self.api(ok=False), mock.patch.object(enrich, "fetch_logo_file",
                                                   return_value=None):
            res = watch.send_alert(self.conn, self.cfg, _row(), "本文", ("t", "1"))
        self.assertEqual((res["ok"], res["photo"], res["message_id"]), (False, False, None))
        self.assertIn("chat not found", res["error"])

    def test_missing_credentials_do_not_touch_telegram(self):
        with self.api() as _:
            res = watch.send_alert(self.conn, self.cfg, _row(), "本文", (None, None))
        self.assertEqual((res["ok"], res["error"]), (False, "credentials missing"))
        self.assertEqual(self.calls, [])

    def test_the_body_is_downloaded_before_sending_when_only_a_url_is_cached(self):
        """URL しか無ければ 2 秒だけ待って本体を落とし、multipart で送る。"""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "logo.png")
            with open(path, "wb") as f:
                f.write(b"\x89PNG\r\n")
            with self.api(), mock.patch.object(enrich, "fetch_logo_file",
                                               return_value=path) as ff:
                res = watch.send_alert(self.conn, self.cfg, _row(), "本文", ("t", "1"))
            self.assertEqual(ff.call_args.kwargs["timeout"], enrich.ALERT_LOGO_TIMEOUT)
            self.assertTrue(res["photo"])
            method, params, files = self.calls[0]
            self.assertEqual(method, "sendPhoto")
            self.assertNotIn("photo", params)        # URL ではなく本体を渡す
            self.assertEqual(files["photo"][0], "logo.png")
            self.assertEqual(files["photo"][1], b"\x89PNG\r\n")
            # 置き場所は tokens に残るので次回はダウンロードしない
            self.assertEqual(db.get_token(self.conn, "bsc", FLAPGUY)["logo_path"], path)


class TestLogoFile(unittest.TestCase):
    """画像本体のダウンロード・保存・再取得の可否。ネットワークには出ない。"""

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.now = 1_000_000
        db.upsert_token(self.conn, "bsc", FLAPGUY, {"symbol": "F"}, self.now)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        patcher = mock.patch.object(enrich, "LOGO_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def urlopen(self, body=b"\x89PNG", mime="image/png"):
        class Res:
            headers = {"Content-Type": mime}

            def read(self, _n=None):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return mock.patch.object(enrich.urllib.request, "urlopen", return_value=Res())

    def test_the_extension_comes_from_the_content_type(self):
        for mime, ext in (("image/png", ".png"), ("image/jpeg", ".jpg"),
                          ("image/webp", ".webp"), ("application/octet-stream", ".png")):
            with self.urlopen(mime=mime):
                path = enrich.fetch_logo_file("https://img/x", os.path.join(self.dir, "a"))
            self.assertTrue(path.endswith(ext), (mime, path))
            self.assertTrue(os.path.isfile(path))

    def test_a_non_http_url_is_never_opened(self):
        with self.urlopen() as op:
            self.assertIsNone(enrich.fetch_logo_file("file:///etc/passwd",
                                                     os.path.join(self.dir, "a")))
        op.assert_not_called()

    def test_a_body_over_the_limit_is_dropped(self):
        with self.urlopen(body=b"x" * 11):
            self.assertIsNone(enrich.fetch_logo_file(
                "https://img/x", os.path.join(self.dir, "a"), max_bytes=10))

    def test_the_local_file_is_preferred_over_the_url(self):
        path = os.path.join(self.dir, "x.png")
        open(path, "wb").close()
        db.save_logo(self.conn, "bsc", FLAPGUY, "https://img/x.png")
        db.save_logo_path(self.conn, "bsc", FLAPGUY, path)
        self.conn.commit()
        self.assertEqual(enrich.logo(self.conn, "bsc", FLAPGUY, now=self.now), path)
        # ファイルが消えていたら URL に落ちる
        os.remove(path)
        self.assertEqual(enrich.logo(self.conn, "bsc", FLAPGUY, now=self.now),
                         "https://img/x.png")

    def test_a_miss_may_be_retried_three_times_with_a_gap(self):
        tok = db.get_token(self.conn, "bsc", FLAPGUY)
        self.assertTrue(enrich.can_fetch_logo(tok, self.now))          # まだ聞いていない
        with mock.patch.object(fourmeme, "image", return_value=None), \
                mock.patch.object(dexscreener, "fetch", return_value=([], 200)) as ds:
            enrich.logo(self.conn, "bsc", FLAPGUY, fetch=True, now=self.now)
            tok = db.get_token(self.conn, "bsc", FLAPGUY)
            self.assertEqual(tok["logo_tries"], 1)
            # 120 秒経つまでは聞き直さない
            self.assertFalse(enrich.can_fetch_logo(tok, self.now + 119))
            self.assertTrue(enrich.can_fetch_logo(tok, self.now + 120))
            enrich.logo(self.conn, "bsc", FLAPGUY, fetch=True, now=self.now + 120)
            enrich.logo(self.conn, "bsc", FLAPGUY, fetch=True, now=self.now + 240)
            tok = db.get_token(self.conn, "bsc", FLAPGUY)
            self.assertEqual(tok["logo_tries"], enrich.LOGO_TRIES)
            self.assertFalse(enrich.can_fetch_logo(tok, self.now + 9999))   # 打ち止め
            self.assertEqual(ds.call_count, 3)

    def test_dexscreener_is_the_fallback_when_fourmeme_has_nothing(self):
        url = "https://cdn.dexscreener.com/x.png"
        with mock.patch.object(fourmeme, "image", return_value=None) as fm, \
                mock.patch.object(dexscreener, "fetch",
                                  return_value=([{"logo": url}], 200)) as ds, \
                mock.patch.object(enrich, "fetch_logo_file", return_value=None):
            self.assertEqual(enrich.logo(self.conn, "bsc", FLAPGUY, fetch=True,
                                         now=self.now), url)
        fm.assert_called_once()
        self.assertEqual(ds.call_args.args[0], "bsc")
        self.assertEqual(db.get_token(self.conn, "bsc", FLAPGUY)["logo"], url)

    def test_other_chains_use_dexscreener_without_fourmeme(self):
        db.upsert_token(self.conn, "arc", "0xarc", {"symbol": "A"}, self.now)
        self.conn.commit()
        url = "https://cdn.dexscreener.com/arc.png"
        with mock.patch.object(fourmeme, "image") as fm, \
                mock.patch.object(dexscreener, "fetch",
                                  return_value=([{"logo": url}], 200)), \
                mock.patch.object(enrich, "fetch_logo_file", return_value=None):
            self.assertEqual(enrich.logo(self.conn, "arc", "0xarc", fetch=True,
                                         now=self.now), url)
        fm.assert_not_called()

    def test_prune_removes_only_the_old_files(self):
        old, new = os.path.join(self.dir, "old.png"), os.path.join(self.dir, "new.png")
        for path in (old, new):
            open(path, "wb").close()
        os.utime(old, (self.now - 20 * 86400, self.now - 20 * 86400))
        os.utime(new, (self.now - 86400, self.now - 86400))
        self.assertEqual(enrich.prune_logo_files(self.now, 14), 1)
        self.assertEqual(os.listdir(self.dir), ["new.png"])
