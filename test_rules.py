"""rules.evaluate の発火条件を合成データで固定する（GMGN は呼ばない）。

しきい値はテスト内の CFG に固定する（実 config.json を読むと、本人が Telegram の
/set でしきい値を変えた瞬間にテストが落ちるため）。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rules  # noqa: E402

NOW = 1_800_000_000
DAY = 86400

CFG = {
    "filters": {"min_liquidity_usd": 10000, "max_market_cap_usd": 500000000,
                "min_holders": 30,
                "size_any": {"mcap": 100000, "vol24h": 300000, "vol5m": 30000}},
    "s1": {"pc1h_pct": 80, "pc5m_pct": 25, "min_buys_5m": 30, "cooldown_h": 2},
    "s2": {"min_vol5m_usd": 20000, "baseline_mult": 3, "baseline_from_min": 15,
           "baseline_to_min": 75, "no_baseline_vol5m_usd": 60000, "cooldown_h": 2},
    "s3": {"min_buys_5m": 300, "buy_sell_ratio": 1.0, "cooldown_h": 2},
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
}


def token(**kw):
    t = {"creation_ts": NOW - 3 * DAY, "first_seen_ts": NOW - 3600,
         "first_price": 1.0, "first_mcap": 200_000}
    t.update(kw)
    return t


def row(**kw):
    """どのトリガーにも当たらない「静かな銘柄」を既定にする。"""
    r = {"price": 1.0, "mcap": 200_000, "liquidity": 50_000,
         "buys5m": 10, "sells5m": 10, "holders": 1_000, "smart": 0,
         "renowned": 0, "visiting": 10, "vol5m": 5_000.0,
         "pc5m": 0.0, "pc1h": 0.0, "pc24h": 0.0, "wash": False}
    r.update(kw)
    return r


def hist(fired=None, snaps=(), recent=()):
    return {"fired": fired or {}, "snapshots": [dict(s) for s in snaps],
            "recent_triggers": set(recent)}


def snap(mins=2, **kw):
    """直前スナップショット（既定は 2 分前・時価総額 $200K）。"""
    s = {"ts": NOW - mins * 60, "mcap": 200_000, "price": 1.0, "holders": 1_000,
         "smart": 0, "visiting": 10}
    s.update(kw)
    return s


def fired(trigger, stages=(), last_ts=None):
    return {trigger: {"stages": {str(s) for s in stages}, "last_ts": last_ts}}


def ev(t, h, r, now=NOW):
    return rules.evaluate(t, h, r, now, CFG)


def notified(events):
    return [e for e in events if e.notify]


def triggers(events):
    return sorted(e.trigger for e in notified(events))


# --- 急増（S） -------------------------------------------------------------

class TestS1PriceSurge(unittest.TestCase):
    def test_fires_on_1h(self):
        e = notified(ev(token(), hist(), row(pc1h=120.0, buys5m=40)))
        self.assertEqual([x.trigger for x in e], ["S1"])
        self.assertEqual(e[0].text, "価格急騰 1h +120%")

    def test_fires_on_5m(self):
        e = notified(ev(token(), hist(), row(pc5m=30.0, buys5m=40)))
        self.assertEqual(e[0].text, "価格急騰 5 分 +30%")

    def test_needs_buys(self):
        self.assertEqual(notified(ev(token(), hist(), row(pc1h=120.0, buys5m=29))), [])

    def test_below_threshold(self):
        self.assertEqual(notified(ev(token(), hist(), row(pc1h=79.0, pc5m=24.0, buys5m=99))), [])

    def test_cooldown(self):
        h = hist(fired("S1", [], NOW - 3600))
        self.assertEqual(notified(ev(token(), h, row(pc1h=120.0, buys5m=40))), [])

    def test_refires_after_cooldown(self):
        # サージは「また盛り上がったら また通知」。2h 明けたら再発火する
        h = hist(fired("S1", [], NOW - 3 * 3600))
        self.assertEqual(triggers(ev(token(), h, row(pc1h=120.0, buys5m=40))), ["S1"])


class TestS2VolumeSurge(unittest.TestCase):
    def snaps(self, vol):
        return [{"ts": NOW - m * 60, "vol5m": vol} for m in (70, 50, 30, 20)]

    def test_fires_on_baseline_multiple(self):
        e = notified(ev(token(), hist(snaps=self.snaps(5_000)), row(vol5m=25_000)))
        self.assertEqual([x.trigger for x in e], ["S2"])
        self.assertIn("平常比 5x", e[0].text)

    def test_not_enough_multiple(self):
        self.assertEqual(notified(ev(token(), hist(snaps=self.snaps(10_000)),
                                     row(vol5m=25_000))), [])

    def test_absolute_floor(self):
        # 平常比 3 倍でも $20K 未満なら撃たない（小さすぎる銘柄を拾わないため）
        self.assertEqual(notified(ev(token(), hist(snaps=self.snaps(1_000)),
                                     row(vol5m=19_000))), [])

    def test_no_baseline_needs_60k(self):
        self.assertEqual(notified(ev(token(), hist(), row(vol5m=59_000))), [])
        e = notified(ev(token(), hist(), row(vol5m=61_000)))
        self.assertEqual([x.trigger for x in e], ["S2"])
        self.assertIn("初認識", e[0].text)

    def test_baseline_window_only(self):
        # 15 分より新しいスナップショットは平常値に入れない（急増中の値で薄まらないように）
        recent = [{"ts": NOW - 5 * 60, "vol5m": 30_000}]
        e = notified(ev(token(), hist(snaps=recent), row(vol5m=61_000)))
        self.assertIn("初認識", e[0].text)

    def test_cooldown(self):
        h = hist(fired("S2", [], NOW - 3600), snaps=self.snaps(5_000))
        self.assertEqual(notified(ev(token(), h, row(vol5m=25_000))), [])


class TestS3Rush(unittest.TestCase):
    def test_fires(self):
        e = notified(ev(token(), hist(), row(buys5m=320, sells5m=150)))
        self.assertEqual([x.trigger for x in e], ["S3"])
        self.assertEqual(e[0].text, "殺到 5 分で買 320")

    def test_below_threshold(self):
        self.assertEqual(notified(ev(token(), hist(), row(buys5m=299, sells5m=10))), [])

    def test_more_sells_than_buys(self):
        self.assertEqual(notified(ev(token(), hist(), row(buys5m=320, sells5m=400))), [])

    def test_wash_trading_suppresses(self):
        r = row(buys5m=320, sells5m=150, wash=True)
        self.assertEqual(notified(ev(token(), hist(), r)), [])

    def test_cooldown(self):
        h = hist(fired("S3", [], NOW - 3600))
        self.assertEqual(notified(ev(token(), h, row(buys5m=320, sells5m=150))), [])


class TestS4Holders(unittest.TestCase):
    def snap(self, mins, holders):
        return {"ts": NOW - mins * 60, "holders": holders}

    def test_fires(self):
        h = hist(snaps=[self.snap(31, 200)])
        e = notified(ev(token(), h, row(holders=240)))
        self.assertEqual([x.trigger for x in e], ["S4"])
        self.assertEqual(e[0].text, "保有者急増 +20%/30 分（200→240）")

    def test_percent_but_too_few_people(self):
        # +20% でも +8 人では撃たない（極小銘柄のノイズ）
        h = hist(snaps=[self.snap(31, 40)])
        self.assertEqual(notified(ev(token(), h, row(holders=48))), [])

    def test_people_but_too_small_percent(self):
        h = hist(snaps=[self.snap(31, 10_000)])
        self.assertEqual(notified(ev(token(), h, row(holders=10_100))), [])

    def test_no_baseline_in_window(self):
        h = hist(snaps=[self.snap(10, 1_000)])
        self.assertEqual(notified(ev(token(), h, row(holders=5_000))), [])

    def test_cooldown(self):
        h = hist(fired("S4", [], NOW - 3600), snaps=[self.snap(31, 200)])
        self.assertEqual(notified(ev(token(), h, row(holders=240))), [])


class TestS5SmartMoney(unittest.TestCase):
    def test_fires(self):
        h = hist(snaps=[snap(mins=31, smart=3)])
        e = notified(ev(token(), h, row(smart=6)))
        self.assertEqual([x.trigger for x in e], ["S5"])
        self.assertEqual(e[0].text, "スマートマネー +3（今 6）")

    def test_increase_of_two_is_not_enough(self):
        h = hist(snaps=[snap(mins=31, smart=4)])
        self.assertEqual(notified(ev(token(), h, row(smart=6))), [])

    def test_below_min_smart(self):
        h = hist(snaps=[snap(mins=31, smart=0)])
        self.assertEqual(notified(ev(token(), h, row(smart=4))), [])

    def test_needs_baseline_in_window(self):
        h = hist(snaps=[snap(mins=2, smart=0)])
        self.assertEqual(notified(ev(token(), h, row(smart=9))), [])

    def test_cooldown(self):
        h = hist(fired("S5", [], NOW - 3 * 3600), snaps=[snap(mins=31, smart=3)])
        self.assertEqual(notified(ev(token(), h, row(smart=6))), [])


class TestS6Attention(unittest.TestCase):
    def test_fires(self):
        h = hist(snaps=[snap(mins=31, visiting=200)])
        e = notified(ev(token(), h, row(visiting=500)))
        self.assertEqual([x.trigger for x in e], ["S6"])
        self.assertEqual(e[0].text, "注目急増 500（30 分前 200）")

    def test_needs_absolute_floor(self):
        h = hist(snaps=[snap(mins=31, visiting=50)])
        self.assertEqual(notified(ev(token(), h, row(visiting=290))), [])

    def test_needs_double(self):
        h = hist(snaps=[snap(mins=31, visiting=400)])
        self.assertEqual(notified(ev(token(), h, row(visiting=700))), [])

    def test_cooldown(self):
        h = hist(fired("S6", [], NOW - 3 * 3600), snaps=[snap(mins=31, visiting=200)])
        self.assertEqual(notified(ev(token(), h, row(visiting=500))), [])


# --- マイルストーン（M） -----------------------------------------------------

class TestM1MarketCap(unittest.TestCase):
    def test_fires_on_observed_crossing(self):
        e = notified(ev(token(), hist(snaps=[snap()]), row(mcap=300_000, pc1h=20.0)))
        self.assertEqual([x.trigger for x in e], ["M1"])
        self.assertEqual(e[0].stage, "250000")
        self.assertIn("時価総額 $250K 到達", e[0].text)

    def test_first_sighting_never_fires(self):
        evs = ev(token(), hist(), row(mcap=300_000, pc1h=20.0))
        self.assertEqual(notified(evs), [])
        self.assertEqual([x.stage for x in evs], ["250000"])  # 段は消費する

    def test_new_token_first_sighting_also_never_fires(self):
        # 若い銘柄の初認識例外は廃止（2026-09-18）
        t = token(creation_ts=NOW - 6 * 3600)
        self.assertEqual(notified(ev(t, hist(), row(mcap=300_000, pc1h=50.0))), [])

    def test_needs_rising_last_hour(self):
        # 跨いでも 1h が下げているなら「盛り上がっている」ではない
        evs = ev(token(), hist(snaps=[snap()]), row(mcap=300_000, pc1h=-5.0))
        self.assertEqual(notified(evs), [])
        self.assertEqual([x.stage for x in evs], ["250000"])

    def test_only_top_step_when_jumping(self):
        evs = ev(token(), hist(snaps=[snap()]), row(mcap=3_500_000, pc1h=50.0))
        self.assertEqual([x.stage for x in notified(evs)], ["3000000"])
        self.assertEqual(sorted(x.stage for x in evs if not x.notify),
                         ["1000000", "250000", "500000"])

    def test_no_double_fire_on_same_step(self):
        h = hist(fired("M1", ["250000"], NOW - DAY), snaps=[snap()])
        self.assertEqual(notified(ev(token(), h, row(mcap=300_000, pc1h=20.0))), [])

    def test_next_step_still_fires(self):
        h = hist(fired("M1", ["250000"], NOW - DAY), snaps=[snap()])
        e = notified(ev(token(), h, row(mcap=600_000, pc1h=20.0)))
        self.assertEqual([x.stage for x in e], ["500000"])

    def test_no_crossing_not_fired(self):
        evs = ev(token(), hist(snaps=[snap(mcap=300_000)]), row(mcap=310_000, pc1h=20.0))
        self.assertEqual(notified(evs), [])
        self.assertEqual([x.stage for x in evs], ["250000"])

    def test_old_token_not_evaluated(self):
        t = token(creation_ts=NOW - 40 * DAY)
        self.assertEqual(notified(ev(t, hist(snaps=[snap()]),
                                     row(mcap=300_000, pc1h=20.0))), [])

    def test_unknown_creation_not_evaluated(self):
        t = token(creation_ts=0)
        self.assertEqual(ev(t, hist(snaps=[snap()]), row(mcap=300_000, pc1h=20.0)), [])


class TestM2Multiple(unittest.TestCase):
    def test_fires(self):
        e = notified(ev(token(), hist(snaps=[snap()]), row(price=3.2, pc1h=50.0)))
        self.assertEqual([x.stage for x in e], ["3"])
        self.assertEqual(e[0].text, "3x 到達")

    def test_only_top_step_when_jumping(self):
        evs = ev(token(), hist(snaps=[snap()]), row(price=12.0, pc1h=50.0))
        self.assertEqual([x.stage for x in notified(evs)], ["10"])
        self.assertEqual(sorted(x.stage for x in evs if not x.notify), ["3"])

    def test_no_double_fire(self):
        h = hist(fired("M2", ["3"], NOW - DAY), snaps=[snap()])
        self.assertEqual(notified(ev(token(), h, row(price=3.5, pc1h=50.0))), [])

    def test_needs_first_price(self):
        self.assertEqual(notified(ev(token(first_price=0), hist(snaps=[snap()]),
                                     row(price=99.0, pc1h=50.0))), [])

    def test_first_sighting_not_fired(self):
        self.assertEqual(notified(ev(token(), hist(), row(price=12.0, pc1h=50.0))), [])

    def test_no_crossing_not_fired(self):
        # 直前スナップショットで既に 3x を超えていれば跨ぎではない
        evs = ev(token(), hist(snaps=[snap(price=3.1)]), row(price=3.5, pc1h=50.0))
        self.assertEqual(notified(evs), [])


class TestM3Volume24h(unittest.TestCase):
    def test_fires_on_observed_crossing(self):
        h = hist(snaps=[snap(vol24h=600_000)])
        e = notified(ev(token(), h, row(vol24h=784_000, pc1h=20.0)))
        self.assertEqual([x.trigger for x in e], ["M3"])
        self.assertEqual(e[0].stage, "700000")
        self.assertEqual(e[0].text, "24h 出来高 $700K 到達（今 $784K）")

    def test_first_sighting_not_fired(self):
        evs = ev(token(), hist(), row(vol24h=784_000, pc1h=20.0))
        self.assertEqual(notified(evs), [])
        self.assertEqual([x.stage for x in evs], ["700000"])

    def test_uses_last_snapshot_with_vol24h(self):
        # 24h は 2 サイクルに 1 回しか取らないので、値を持つ最後のスナップショットと比べる
        h = hist(snaps=[snap(mins=6, vol24h=600_000), snap(mins=2)])
        self.assertEqual(triggers(ev(token(), h, row(vol24h=784_000, pc1h=20.0))), ["M3"])

    def test_no_double_fire_on_same_step(self):
        h = hist(fired("M3", ["700000"], NOW - DAY), snaps=[snap(vol24h=600_000)])
        self.assertEqual(notified(ev(token(), h, row(vol24h=800_000, pc1h=20.0))), [])

    def test_old_token_not_evaluated(self):
        t = token(creation_ts=NOW - 40 * DAY)
        h = hist(snaps=[snap(vol24h=600_000)])
        self.assertEqual(notified(ev(t, h, row(vol24h=784_000, pc1h=20.0))), [])


class TestM4OldRevival(unittest.TestCase):
    def test_fires(self):
        t = token(creation_ts=NOW - 90 * DAY)
        e = notified(ev(t, hist(), row(pc24h=240.0, pc1h=5.0)))
        self.assertEqual([x.trigger for x in e], ["M4"])
        self.assertEqual(e[0].text, "古参復活 24h +240%")

    def test_young_token_excluded(self):
        self.assertEqual(notified(ev(token(), hist(), row(pc24h=240.0, pc1h=5.0))), [])

    def test_below_threshold(self):
        t = token(creation_ts=NOW - 90 * DAY)
        self.assertEqual(notified(ev(t, hist(), row(pc24h=150.0, pc1h=5.0))), [])

    def test_needs_rising_last_hour(self):
        t = token(creation_ts=NOW - 90 * DAY)
        self.assertEqual(notified(ev(t, hist(), row(pc24h=240.0, pc1h=-3.0))), [])

    def test_cooldown(self):
        t = token(creation_ts=NOW - 90 * DAY)
        h = hist(fired("M4", [], NOW - 5 * 3600))
        self.assertEqual(notified(ev(t, h, row(pc24h=240.0, pc1h=5.0))), [])


# --- 共通 -----------------------------------------------------------------

class TestFilters(unittest.TestCase):
    def test_low_liquidity(self):
        r = row(mcap=3_000_000, liquidity=5_000, buys5m=999, sells5m=1)
        self.assertEqual(ev(token(), hist(), r), [])

    def test_huge_market_cap(self):
        r = row(mcap=600_000_000, buys5m=999, sells5m=1)
        self.assertEqual(ev(token(), hist(), r), [])

    def test_too_few_holders(self):
        r = row(holders=29, buys5m=999, sells5m=1)
        self.assertEqual(ev(token(), hist(), r), [])

    def test_honeypot_is_not_filtered(self):
        # honeypot は表示するだけで除外しない
        r = row(buys5m=320, sells5m=150, honeypot=1)
        self.assertEqual(triggers(ev(token(), hist(), r)), ["S3"])

    def test_missing_fields_do_not_filter(self):
        r = row(mcap=300_000, liquidity=None, pc1h=20.0)
        self.assertEqual(triggers(ev(token(), hist(snaps=[snap()]), r)), ["M1"])

    def test_too_small_for_any_size(self):
        # 時価総額 $50K・24h $100K・5 分 $1K はどの下限にも届かないので評価しない
        r = row(mcap=50_000, vol24h=100_000, vol5m=1_000, buys5m=999, sells5m=1)
        self.assertEqual(ev(token(), hist(), r), [])

    def test_one_size_is_enough(self):
        # 時価総額が小さくても 5 分出来高が $30K 以上なら評価する
        r = row(mcap=50_000, vol24h=None, vol5m=30_000, buys5m=999, sells5m=1)
        self.assertEqual(triggers(ev(token(), hist(), r)), ["S3"])

    def test_all_size_fields_missing_is_not_evaluated(self):
        r = row(mcap=None, vol24h=None, vol5m=None, buys5m=999, sells5m=1)
        self.assertEqual(ev(token(), hist(), r), [])


class TestHeatAndYabai(unittest.TestCase):
    def test_heat_counts_distinct_triggers_in_24h(self):
        h = hist(recent=["S3", "S5"], snaps=[snap()])
        e = notified(ev(token(), h, row(mcap=300_000, pc1h=20.0)))[0]
        self.assertEqual(e.heat, 3)
        self.assertFalse(e.yabai)

    def test_heat_ignores_retired_trigger_names(self):
        # ルール変更前の T1〜T8 が直近 24h に残っていても熱度には数えない
        h = hist(recent=["T1", "T4", "T6", "T8"])
        e = notified(ev(token(), h, row(buys5m=320, sells5m=150)))[0]
        self.assertEqual(e.heat, 1)
        self.assertFalse(e.yabai)

    def test_heat_does_not_double_count_same_trigger(self):
        h = hist(fired("S3", [], NOW - 3 * 3600), recent=["S3"])
        e = notified(ev(token(), h, row(buys5m=320, sells5m=150)))[0]
        self.assertEqual(e.heat, 1)

    def test_yabai_on_mcap_step(self):
        e = notified(ev(token(), hist(snaps=[snap()]),
                        row(mcap=1_200_000, pc1h=20.0)))[0]
        self.assertTrue(e.yabai)
        self.assertEqual(e.stage, "1000000")

    def test_yabai_on_10x(self):
        e = notified(ev(token(), hist(snaps=[snap()]), row(price=11.0, pc1h=50.0)))
        self.assertTrue(all(x.yabai for x in e))

    def test_not_yabai_on_3x(self):
        e = notified(ev(token(), hist(snaps=[snap()]), row(price=3.2, pc1h=50.0)))
        self.assertFalse(any(x.yabai for x in e))

    def test_yabai_on_volume_step(self):
        h = hist(snaps=[snap(vol24h=600_000)])
        e = notified(ev(token(), h, row(vol24h=1_500_000, pc1h=20.0)))[0]
        self.assertEqual(e.stage, "1000000")
        self.assertTrue(e.yabai)

    def test_yabai_on_heat(self):
        h = hist(recent=["S1", "S2", "M1"])
        e = notified(ev(token(), h, row(buys5m=320, sells5m=150)))[0]
        self.assertEqual(e.heat, 4)
        self.assertTrue(e.yabai)

    def test_multiple_triggers_share_heat(self):
        h = hist(snaps=[snap(mins=31, holders=200, smart=3, visiting=200),
                        snap(mins=2, mcap=200_000)])
        evs = notified(ev(token(), h, row(mcap=300_000, pc1h=90.0, buys5m=320, sells5m=150,
                                          holders=260, smart=6,
                                          visiting=500)))
        self.assertEqual(sorted(x.trigger for x in evs),
                         ["M1", "S1", "S3", "S4", "S5", "S6"])
        self.assertEqual({x.heat for x in evs}, {6})
        self.assertTrue(all(x.yabai for x in evs))


class TestMilestoneActivity(unittest.TestCase):
    """到達（M）は「いま動いている」ときだけ。5 分出来高 $210 の 3x などを撃たない。"""

    def test_quiet_token_does_not_reach(self):
        r = row(mcap=300_000, pc1h=20.0, vol5m=210.0, buys5m=2)
        self.assertEqual(ev(token(), hist(snaps=[snap()]), r), [])

    def test_too_few_buys(self):
        r = row(mcap=300_000, pc1h=20.0, vol5m=50_000.0, buys5m=4)
        self.assertEqual([e for e in ev(token(), hist(snaps=[snap()]), r)
                          if e.trigger.startswith("M")], [])

    def test_lively_token_reaches(self):
        r = row(mcap=300_000, pc1h=20.0, vol5m=5_000.0, buys5m=10)
        self.assertEqual(triggers(ev(token(), hist(snaps=[snap()]), r)), ["M1"])


class TestQuietTokenStaysQuiet(unittest.TestCase):
    def test_settled_large_cap_is_not_fired(self):
        """落ち着いて高い時価総額を保っているだけの銘柄は撃たない（$ORBIO の再現）。

        旧 T8 は初認識で「24h 出来高 $3M」を撃っていた。新ルールでは段を黙って消費するだけで
        通知は 0 件になる。
        """
        t = token(creation_ts=NOW - 10 * DAY, first_price=1.0, first_mcap=48_600_000)
        snaps = [snap(mins=m, mcap=48_600_000, price=1.0, holders=12_000, smart=2,
                      visiting=120, vol5m=15_000, vol24h=3_000_000)
                 for m in (70, 50, 31, 10, 2)]
        r = row(mcap=48_600_000, vol24h=3_000_000, vol5m=16_000,
                holders=12_000, buys5m=5, sells5m=15, liquidity=900_000,
                smart=2, visiting=120, pc5m=-0.4, pc1h=-1.2, pc24h=3.0)
        evs = ev(t, hist(snaps=snaps), r)
        self.assertEqual(notified(evs), [])
        self.assertTrue(all(e.text == "" for e in evs))


class TestFormat(unittest.TestCase):
    def test_usd(self):
        self.assertEqual(rules.fmt_usd(1_200_000), "$1.2M")
        self.assertEqual(rules.fmt_usd(210_000), "$210K")
        self.assertEqual(rules.fmt_usd(3_000_000), "$3M")
        self.assertEqual(rules.fmt_usd(None), "?")

    def test_x_and_duration(self):
        self.assertEqual(rules.fmt_x(5.72), "5.7x")
        self.assertEqual(rules.fmt_x(10.0), "10x")
        self.assertEqual(rules.fmt_dur(38 * 60), "38 分")
        self.assertEqual(rules.fmt_dur(2 * 3600), "2h")
        self.assertEqual(rules.fmt_dur(3 * 86400), "3 日")


if __name__ == "__main__":
    unittest.main()
