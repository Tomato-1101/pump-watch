#!/usr/bin/env bash
# launchd / cron から呼ぶラッパー。gmgn ラッパー（~/.local/bin/gmgn）に PATH を通してから実行する。
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$(dirname "$0")"
# 第 1 引数が hot なら盛り上がり確定レーン（hot.py cycle / digest / judge）
if [[ "$1" == "hot" ]]; then
  shift
  exec python3 hot.py "$@"
fi
exec python3 watch.py "$@"
