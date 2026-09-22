#!/bin/sh
# upsmgr 容器入口：NUT 自包含服务（首次启动 apt 装 + 写配置 + 起 daemon） -> upsmgr 主程序
# 设计：
#   1. 容器镜像用 python:3.12-slim-bookworm（无 NUT），首次启动时 apt 装 nut-server+nut-client；
#      装包产物在容器层，容器被 docker rm 后丢失，所以每次启动都 command -v upsc 检查
#   2. NUT 配置文件每次启动都重写（容器重建 /etc/nut 丢失）
#   3. upsdrvctl（usbhid-ups 驱动）+ upsd 后台 daemon，由本脚本拉起；upsmgr 用 upsc 取字段
#   4. 若 NUT 不识别当前 UPS（如 WalleCube W150 不在 usbhid-ups subdriver 表），
#      upsdrvctl 失败但 upsmgr 的 auto 模式会自动 fallback 到自研 hid_native 驱动
#   5. /data 卷持久化装包标记（避免容器重启时重复 apt-get update；不影响"command -v" 检查）

set -e
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

DATA=/data
NUT_CFG=/etc/nut
NUT_FLAG=$DATA/.nut_v1_installed

mkdir -p $DATA $NUT_CFG /var/run/nut 2>/dev/null || true

# ---- 1. upsmgr 配置兜底 ----
if [ ! -f $DATA/upsmgr.json ]; then
  cat > $DATA/upsmgr.json <<'EOF'
{
  "port": 9750,
  "driver": "auto",
  "driver_preferred": "nut",
  "poll_sec": 10,
  "low_batt_pct": 50,
  "shutdown_batt_pct": 30,
  "shutdown_delay_min": 5,
  "enable_shutdown": false,
  "dry_run": true,
  "shutdown_command": "/sbin/shutdown -h now",
  "pre_shutdown_commands": [],
  "password": "admin",
  "history_days": 7,
  "notify_enabled": true,
  "notify_uid": 1000
}
EOF
fi

# ---- 2. NUT 装包（容器层丢就重装；标记文件避免每次都跑 sed 切源） ----
if ! command -v upsc >/dev/null 2>&1; then
  echo "=== installing nut-server + nut-client (one-time per container layer) ==="
  # 用 tuna 镜像加速 apt（python:3.12-slim-bookworm 默认 deb.debian.org 国内可能慢）
  if [ ! -f $NUT_FLAG ]; then
    sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
      /etc/apt/sources.list.d/debian.sources 2>/dev/null \
      || sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
           /etc/apt/sources.list 2>/dev/null || true
    touch $NUT_FLAG
  fi
  apt-get update
  apt-get install -y --no-install-recommends nut-server nut-client procps
  rm -rf /var/lib/apt/lists/*
fi

# ---- 3. NUT 配置（容器重建 /etc/nut 丢失，每次重写） ----
# ups.conf: usbhid-ups 是 NUT 通用 HID Power Device 驱动；port=auto + vendorid/productid 锁定 WalleCube
# user=root 绕开默认 nut 用户对 /dev/hidraw0 /dev/bus/usb 的权限不足
cat > $NUT_CFG/ups.conf <<'EOF'
[wallecube]
    driver = usbhid-ups
    port = auto
    vendorid = 04d8
    productid = d005
    desc = "WalleCube Smart UPS W150"
    user = root
EOF

# upsd.conf: 只监听本地，避免被外部直接访问
cat > $NUT_CFG/upsd.conf <<'EOF'
LISTEN 127.0.0.1 3493
EOF

# upsd.users: upsc 取字段要的凭据（本地内，密码不强求）
cat > $NUT_CFG/upsd.users <<'EOF'
[admin]
    password = upsmgr
    upsmon master
EOF

# upsmon.conf: 不用 upsmon（upsmgr 自己做策略），但 NUT 包要求文件存在
cat > $NUT_CFG/upsmon.conf <<'EOF'
RUN_AS_USER root
EOF

# nut.conf: Debian NUT 包的开关（MODE=none 时 upsd 拒绝启动）
cat > $NUT_CFG/nut.conf <<'EOF'
MODE=standalone
EOF

# 权限：upsd 默认以 nut 用户跑，/var/run/nut 要可写；保险起见 chown root
chown -R root:root /var/run/nut 2>/dev/null || true
chmod 0755 $NUT_CFG 2>/dev/null || true
chmod 0640 $NUT_CFG/ups.conf $NUT_CFG/upsd.users 2>/dev/null || true

# 给 NUT libusb/usbhid-ups 足够权限（容器虽 privileged 但 hidraw0 默认 600，/dev/bus/usb 节点也是 root 644）
# 容器内 root 已能直接读，但保险 chmod 给所有用户可读（容器本身 privileged，无新增风险）
chmod 0666 /dev/hidraw* 2>/dev/null || true
find /dev/bus/usb -type c -exec chmod 0666 {} \; 2>/dev/null || true

# ---- 4. 起 NUT daemon（先杀残留，避免端口/设备占用） ----
pkill -f 'upsd$' 2>/dev/null || true
pkill -f 'usbhid-ups' 2>/dev/null || true
sleep 1

echo "===hid devices: $(ls /dev/hidraw* 2>/dev/null || echo none)"

# usbhid-ups 驱动：如果 NUT 不识别当前 UPS（WalleCube 大概率不在 mfr subdriver 表），
# 此命令会失败；不影响后续 upsmgr fallback 到 hid_native
echo "=== starting NUT usbhid-ups driver ==="
upsdrvctl start 2>&1 || echo "WARN: upsdrvctl failed - NUT may not support this UPS; upsmgr will fallback to hid_native"

echo "=== starting upsd ==="
upsd -u root 2>&1 || echo "WARN: upsd start failed"

sleep 1

echo "=== NUT upsc test (wallecube@127.0.0.1:3493) ==="
upsc wallecube@127.0.0.1:3493 2>&1 | head -8 || echo "no NUT data (expected if NUT does not support this UPS)"

# ---- 5. 启动 upsmgr 主程序 ----
exec python3 -u /opt/app/upsmgr.py
