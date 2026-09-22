#!/bin/bash
# 去掉铁牛消息中心通知的「测试消息」前缀：等长补丁系统 filemanage 二进制里的 TestMsg 模板。
# 幂等可重复执行；停服→原地改写→启服，权限位显式恢复；自动备份到 /volume1/data/filemanage.bak.latest。
# 系统升级会还原二进制 → 前缀回来后重跑本脚本即可；还原补丁 = 把备份拷回原位。
# 用法：sudo bash patch_msg_prefix.sh
BIN=/usr/device/filemanage/filemanage
[ -f "$BIN" ] || { echo "[msgprefix] $BIN not found, skip"; exit 0; }

python3 - "$BIN" <<'PYEOF'
import sys, os, shutil
BIN = sys.argv[1]
OLD = '测试消息{{.Desc}}'.encode('utf-8')   # 21B 模板原文（含前缀）
NEW = b'{{.Desc}}' + b' ' * 12              # 21B 等长替换，尾部空格渲染不可见
data = open(BIN, 'rb').read()
if OLD not in data:
    print('[msgprefix] already patched or template absent, skip')
    sys.exit(0)
bak = '/volume1/data/filemanage.bak.latest'
if not os.path.exists(bak):
    shutil.copy2(BIN, bak)
    print('[msgprefix] backup ->', bak)
os.chmod(BIN, 0o755)
print('[msgprefix] need-patch')
sys.exit(10)
PYEOF
RC=$?
[ "$RC" -ne 10 ] && exit 0

echo "[msgprefix] stop AiNasFilemanage ..."
systemctl stop AiNasFilemanage || true

python3 - "$BIN" <<'PYEOF'
import sys, os
BIN = sys.argv[1]
OLD = '测试消息{{.Desc}}'.encode('utf-8')
NEW = b'{{.Desc}}' + b' ' * 12
data = open(BIN, 'rb').read()
mode = os.stat(BIN).st_mode
n = 0
with open(BIN, 'r+b') as f:   # 原地等长改写（服务已停，不会 ETXTBSY；不 rename 不丢权限位）
    i = data.find(OLD)
    while i >= 0:
        f.seek(i); f.write(NEW); n += 1
        i = data.find(OLD, i + 1)
os.chmod(BIN, mode | 0o111)
print('[msgprefix] patched %d site(s)' % n)
PYEOF

systemctl start AiNasFilemanage || true
sleep 3
systemctl is-active AiNasFilemanage || true
echo "[msgprefix] done"
exit 0
