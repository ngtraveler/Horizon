#!/data/data/com.termux/files/usr/bin/bash
# Install Marine Radar on an Android phone inside Termux.
#
#   bash dashboard/termux-setup.sh
#
# Installs Python, copies the dashboard to ~/marine-radar, collects one snapshot,
# writes a Termux:Boot hook and starts the local server on 127.0.0.1:8899.
#
# Notes:
#   * Install Termux from F-Droid (the Play Store build is abandoned).
#   * Install Termux:Boot and open it once so the service survives reboots.
#   * Set Termux battery usage to "Unrestricted", otherwise Android kills it.
#   * Traffic: one collection is ~3.5 MB. INTERVAL defaults to 3600 s (~85 MB/day).
#
# Override the refresh interval:
#   INTERVAL=1800 bash dashboard/termux-setup.sh

set -euo pipefail

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${MARINE_RADAR_HOME:-$HOME/marine-radar}"
INTERVAL="${INTERVAL:-3600}"
PORT="${PORT:-8899}"

if [[ ! -r "$KIT/data/config.json" ]]; then
  echo "cannot read $KIT/data/config.json" >&2
  echo "if the kit sits in shared storage, run 'termux-setup-storage' first" >&2
  exit 1
fi

echo "kit  : $KIT"
echo "dest : $DEST"

if ! command -v python >/dev/null 2>&1; then
  echo "installing python ..."
  pkg update -y >/dev/null 2>&1 || true
  pkg install -y python ca-certificates
fi
pkg install -y ca-certificates >/dev/null 2>&1 || true
python -c 'import sys; print("python", sys.version.split()[0])'

mkdir -p "$DEST/dashboard" "$DEST/data/summaries"
cp -R "$KIT/dashboard/." "$DEST/dashboard/"
cp "$KIT/data/config.json" "$DEST/config.json"

# Keep the CPU awake while the screen is off (needs Termux:API for best results).
termux-wake-lock >/dev/null 2>&1 || echo "warning: termux-wake-lock unavailable (install Termux:API)"

echo "collecting first snapshot (~3.5 MB) ..."
python "$DEST/dashboard/collect.py" \
  -c "$DEST/config.json" \
  --summaries "$DEST/data/summaries" \
  --hours 48

# --- boot hook --------------------------------------------------------------
BOOT_DIR="$HOME/.termux/boot"
mkdir -p "$BOOT_DIR"
cat > "$BOOT_DIR/marine-radar.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock >/dev/null 2>&1
cd "$DEST" || exit 1
nohup sh -c 'while true; do python dashboard/collect.py -c config.json --summaries data/summaries --hours 48 >> collect.log 2>&1; sleep $INTERVAL; done' >/dev/null 2>&1 &
nohup python dashboard/serve.py --port $PORT --bind 127.0.0.1 >> serve.log 2>&1 &
EOF
chmod +x "$BOOT_DIR/marine-radar.sh"

# --- start now --------------------------------------------------------------
pkill -f "serve.py --port $PORT" 2>/dev/null || true
nohup python "$DEST/dashboard/serve.py" --port "$PORT" --bind 127.0.0.1 \
  >> "$DEST/serve.log" 2>&1 &
sleep 1

cat <<NEXT

已启动。

  面板地址 : http://127.0.0.1:$PORT/index.html
  数据目录 : $DEST
  刷新间隔 : ${INTERVAL}s
  开机自启 : $BOOT_DIR/marine-radar.sh（需装 Termux:Boot 并打开一次）

常用：
  pkill -f serve.py           # 停止面板
  tail -f $DEST/serve.log     # 看日志
  INTERVAL=1800 bash dashboard/termux-setup.sh   # 改成 30 分钟刷新

提示：一次采集约 3.5 MB。手机上建议 30-60 分钟一次，或把三个体积最大的
UK 消费类源关掉（Motor Boat & Yachting / Practical Boat Owner / Yachting World，
合计约占 2.4 MB），能省掉 68% 流量。
NEXT
