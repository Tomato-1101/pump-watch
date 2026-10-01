"""常駐 bot（bot.py）の振り分けとボタン。Telegram にも config.json にも触らない。

送信は notify.api をモックして「何を・どの API へ投げたか」だけ見る。設定の保存は
watch.save_config をモックして実ファイルを書き換えないようにする
（本人のしきい値を壊さないため。しきい値の変更そのものは set_config_value 側で検証済み）。
"""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bot     # noqa: E402
import db      # noqa: E402
import notify  # noqa: E402
import watch   # noqa: E402

OWNER = "12345"


def cfg():
    return json.loads(json.dumps(watch.DEFAULTS))


class Telegram:
    """notify.api の差し替え。呼ばれた method と params を貯める。"""

    def __init__(self):
        self.calls = []

    def __call__(self, token, method, params, timeout=30, files=None):
        self.calls.append((method, params))
        return {"message_id": 7}, None

    def methods(self):
        return [m for m, _ in self.calls]

    def texts(self):
        return [p.get("text", "") for _, p in self.calls]


class BotCase(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.tg = Telegram()
        self.saved = []
        patches = [
            mock.patch.object(notify, "api", self.tg),
            mock.patch.object(watch, "save_config", self.saved.append),
            mock.patch.object(watch, "load_config", cfg),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.conn.close()

    def message(self, text, chat=OWNER):
        bot.handle(self.conn, "tok", OWNER,
                   {"update_id": 1, "message": {"chat": {"id": int(chat)}, "text": text}})

    def press(self, data, chat=OWNER):
        bot.handle(self.conn, "tok", OWNER, {
            "update_id": 1,
            "callback_query": {"id": "cb1", "data": data,
                               "message": {"message_id": 9, "chat": {"id": int(chat)}}},
        })


class TestDispatch(BotCase):
    def test_menu_uses_inline_buttons_not_a_persistent_keyboard(self):
        self.message("/start")
        self.assertEqual(self.tg.methods(), ["sendMessage"])
        params = self.tg.calls[0][1]
        self.assertEqual(params["text"], "ボタンで操作できます。")
        markup = json.loads(params["reply_markup"])
        self.assertNotIn("keyboard", markup)          # 画面下の常設は出さない
        self.assertEqual([[b["text"] for b in row] for row in markup["inline_keyboard"]],
                         [["状態", "近い銘柄"], ["チェーン", "しきい値"], ["送信 ON/OFF", "ヘルプ"]])

    def test_menu_buttons_answer_in_place(self):
        self.press("m:chains")
        self.assertIn("対象チェーン", self.tg.texts()[-1])
        self.press("m:dryrun")
        self.assertIn("通知の送信", self.tg.texts()[-1])
        self.press("m:help")
        self.assertIn("pump-watch のコマンド", self.tg.texts()[-1])

    def test_the_word_menu_works_too(self):
        self.message("メニュー")
        self.assertIn("reply_markup", self.tg.calls[0][1])

    def test_button_text_maps_to_the_command(self):
        self.message("状態")
        self.assertIn("pump-watch 稼働状況", self.tg.texts()[0])
        self.assertIn("更新", self.tg.calls[0][1]["reply_markup"])

    def test_chain_button_opens_the_inline_keyboard(self):
        self.message("チェーン")
        markup = json.loads(self.tg.calls[0][1]["reply_markup"])
        labels = [b["text"] for row in markup["inline_keyboard"] for b in row]
        # 既定の 3 チェーンにチェックが付き、残りは素のまま
        self.assertEqual(labels, ["✓ bsc", "✓ robinhood", "✓ arc", "sol", "base", "eth"])

    def test_help(self):
        self.message("ヘルプ")
        self.assertIn("/menu", self.tg.texts()[0])

    def test_plain_command_falls_through_to_watch(self):
        self.message("/config")
        self.assertIn("s3.min_buys_5m=300", self.tg.texts()[0])

    def test_other_chats_are_ignored(self):
        self.message("/status", chat="999")
        self.assertEqual(self.tg.calls, [])

    def test_callbacks_from_other_chats_are_ignored(self):
        self.press("c:sol", chat="999")
        self.assertEqual(self.tg.calls, [])

    def test_empty_message_is_ignored(self):
        bot.handle(self.conn, "tok", OWNER,
                   {"update_id": 1, "message": {"chat": {"id": int(OWNER)}}})
        self.assertEqual(self.tg.calls, [])


class TestCallbacks(BotCase):
    def test_every_callback_is_answered_first(self):
        self.press("t")
        self.assertEqual(self.tg.methods()[0], "answerCallbackQuery")
        self.assertEqual(self.tg.calls[0][1]["callback_query_id"], "cb1")

    def test_chain_toggle_writes_config_and_refreshes(self):
        self.press("c:sol")
        self.assertEqual(self.saved, [{"chains": ["bsc", "robinhood", "arc", "sol"]}])
        self.assertEqual(self.tg.methods(), ["answerCallbackQuery", "editMessageText"])
        text = self.tg.calls[1][1]["text"]
        self.assertIn("bsc,robinhood,arc,sol", text)
        markup = json.loads(self.tg.calls[1][1]["reply_markup"])
        self.assertIn("✓ sol", [b["text"] for row in markup["inline_keyboard"] for b in row])

    def test_chain_toggle_off(self):
        self.press("c:arc")
        self.assertEqual(self.saved, [{"chains": ["bsc", "robinhood"]}])

    def test_the_last_chain_cannot_be_removed(self):
        with mock.patch.object(watch, "load_config", lambda: dict(cfg(), chains=["bsc"])):
            self.press("c:bsc")
        self.assertEqual(self.saved, [])
        self.assertIn("最低 1 つは残す", self.tg.calls[1][1]["text"])

    def test_dry_run_switch(self):
        self.press("d:on")
        self.assertEqual(self.saved, [{"dry_run": False}])
        text = self.tg.calls[1][1]["text"]
        self.assertIn("送信 ON にしました。両レーンとも次の周期で反映（最大 30 秒）", text)

    def test_dry_run_off(self):
        self.press("d:off")
        self.assertEqual(self.saved, [{"dry_run": True}])
        self.assertIn("送信 OFF にしました", self.tg.calls[1][1]["text"])

    def test_dry_run_toggle_flips_and_shows_the_new_state(self):
        # 既定は dry_run=True（送信 OFF）。トグル 1 押しで ON になり、ボタンの文言も次の状態を示す
        self.press("d:toggle")
        self.assertEqual(self.saved, [{"dry_run": False}])
        params = self.tg.calls[1][1]
        self.assertIn("送信 ON にしました", params["text"])
        self.assertIn("送信 ON（押すと OFF）", params["reply_markup"])
        self.assertIn('"d:toggle"', params["reply_markup"])

    def test_status_has_the_toggle_and_keeps_the_status_screen(self):
        self.press("sd:toggle")
        self.assertEqual(self.saved, [{"dry_run": False}])
        params = self.tg.calls[1][1]
        self.assertIn("pump-watch 稼働状況", params["text"])   # 状態画面のまま書き換わる
        self.assertIn("送信 ON にしました", params["text"])
        self.assertIn('"sd:toggle"', params["reply_markup"])
        self.assertIn('"r:status"', params["reply_markup"])

    def test_threshold_sections_then_keys(self):
        self.press("ts:s1")
        markup = json.loads(self.tg.calls[1][1]["reply_markup"])
        rows = [row[0] for row in markup["inline_keyboard"]]
        # 1 行 1 キー・現在値つき（最後の行はセクション一覧に戻るボタン）
        self.assertEqual(rows[0]["text"], "S1 1h 変化率  80")
        self.assertEqual(rows[0]["callback_data"], "tk:s1.pc1h_pct")
        self.assertEqual(rows[1]["text"], "S1 5 分変化率  25")
        self.assertEqual(rows[-1]["callback_data"], "t")

    def test_threshold_key_shows_value_default_and_meaning(self):
        self.press("tk:s1.pc5m_pct")
        text = self.tg.calls[1][1]["text"]
        self.assertIn("S1 5 分変化率", text)
        self.assertIn("今: 25 ・ 既定: 25", text)
        self.assertIn("5 分でこの % 以上上がっていること", text)
        markup = json.loads(self.tg.calls[1][1]["reply_markup"])
        labels = [b["text"] for row in markup["inline_keyboard"] for b in row]
        self.assertEqual(labels, ["−50%", "−20%", "−10%", "+10%", "+20%", "+50%",
                                  "既定に戻す", "← 戻る"])

    def test_percent_step_saves_and_replies(self):
        self.press("ta:s1.pc5m_pct:20")
        self.assertEqual(self.saved[0]["s1"]["pc5m_pct"], 30)
        self.assertIn("S1 5 分変化率: 25 → 30", self.tg.calls[1][1]["text"])

    def test_percent_step_keeps_floats(self):
        self.press("ta:s3.buy_sell_ratio:50")
        self.assertEqual(self.saved[0]["s3"]["buy_sell_ratio"], 1.5)

    def test_restore_default(self):
        changed = dict(cfg())
        changed["s1"] = dict(changed["s1"], pc5m_pct=99)
        with mock.patch.object(watch, "load_config", lambda: changed):
            self.press("ta:s1.pc5m_pct:def")
        self.assertEqual(self.saved[0]["s1"]["pc5m_pct"], 25)
        self.assertIn("S1 5 分変化率: 99 → 25", self.tg.calls[1][1]["text"])

    def test_default_when_already_default_changes_nothing(self):
        self.press("ta:s1.pc5m_pct:def")
        self.assertEqual(self.saved, [])
        self.assertIn("（変わらず）", self.tg.calls[1][1]["text"])

    def test_refresh_button(self):
        self.press("r:status")
        self.assertEqual(self.tg.methods(), ["answerCallbackQuery", "editMessageText"])
        self.assertIn("pump-watch 稼働状況", self.tg.calls[1][1]["text"])

    def test_unknown_callback_only_answers(self):
        self.press("zzz:1")
        self.assertEqual(self.tg.methods(), ["answerCallbackQuery"])


class TestStepValue(unittest.TestCase):
    def test_integers_stay_integers(self):
        self.assertEqual(bot.step_value(25, 20), 30)
        self.assertEqual(bot.step_value(25, -20), 20)
        self.assertEqual(bot.step_value(300, -50), 150)

    def test_never_reaches_zero(self):
        self.assertEqual(bot.step_value(1, -50), 1)

    def test_floats_keep_a_fraction(self):
        self.assertEqual(bot.step_value(1.0, 10), 1.1)
        self.assertEqual(bot.step_value(2.5, -20), 2.0)


class TestKeyCoverage(unittest.TestCase):
    def test_every_settable_key_has_a_label(self):
        missing = [k for k in bot.numeric_keys(watch.DEFAULTS) if k not in bot.KEYS]
        self.assertEqual(missing, [])

    def test_every_key_lands_in_a_section(self):
        sections = {s for s, _ in bot.SECTIONS}
        for key in bot.numeric_keys(watch.DEFAULTS):
            self.assertIn(bot.section_of(key), sections, key)

    def test_callback_data_fits_telegram(self):
        """callback_data は 64 バイトまで。一番長いキーでも収まること。"""
        for key in bot.numeric_keys(watch.DEFAULTS):
            for prefix in (f"tk:{key}", f"ta:{key}:def", f"ta:{key}:-50"):
                self.assertLessEqual(len(prefix.encode()), 64, prefix)


class TestBotLaneLine(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_no_record(self):
        self.assertIn("停止中", watch.bot_lane_line(self.conn, 1_000_000))

    def test_alive(self):
        db.kv_set(self.conn, "bot_last_poll_ts", 1_000_000 - 5)
        self.assertEqual(watch.bot_lane_line(self.conn, 1_000_000), "bot: 稼働中（最終受信 5 秒前）")

    def test_stale(self):
        db.kv_set(self.conn, "bot_last_poll_ts", 1_000_000 - 600)
        self.assertIn("停止中（最終受信 10 分前）", watch.bot_lane_line(self.conn, 1_000_000))


if __name__ == "__main__":
    unittest.main()
