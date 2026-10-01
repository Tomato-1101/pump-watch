"""データ源アダプタ。どれも rules.ROW_KEYS の正規化行のリストを返す。

- gmgn          : 保有者数・スマートマネー・注目・リスク・launchpad（唯一の供給元）。無料枠が
                  約 35 秒に 1 コールなので「補強」専用。
- geckoterminal : 新規プール・trending の発見（無料・鍵不要。実効上限が低いので少なめに撃つ）。
- dexscreener   : 既知の全銘柄を 30 件ずつ一括更新（300 コール/分・鍵不要）。価格と出来高の主軸。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
