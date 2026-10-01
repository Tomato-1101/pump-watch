"""BSC のオンチェーン高速レーン（ブロックごとに Swap を直集計して数秒で判定する）。

API レーン（watch.py の 20 秒巡回）はデータ源そのものが 30〜60 秒遅れるので
「条件を満たしてから 10 秒以内」には届かない。こちらは公開 RPC の eth_getLogs を
1 ブロック 1 コールで追い、PancakeSwap V2/V3・four.meme・flap の取引を直接集計する。

判定（rules）・状態（db の tokens/snapshots/events）・通知（watch の本文組み立てと
2 段配信）は API レーンと共有する。既存ファイルは読むだけで書き換えない。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
