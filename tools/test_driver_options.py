# -*- coding: utf-8 -*-
"""upsmgr 接口回归测试（本地）：起一个临时实例 -> 调 API -> 校验驱动选项与设置校验。

只依赖标准库，不会碰你的正式配置（用临时目录 + 临时端口）。
用法（仓库根目录下）：
    python tools/test_driver_options.py
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "app", "upsmgr.py")
PORT = 19750          # 用非默认端口，避免撞上正在跑的正式实例
PWD = "admin"


def main():
    if not os.path.isfile(SCRIPT):
        print("[FAIL] 找不到主程序：%s" % SCRIPT)
        return 1

    work = tempfile.mkdtemp(prefix="upsmgr_test_")
    cfg_path = os.path.join(work, "upsmgr.json")
    log_path = os.path.join(work, "run.log")
    env = dict(os.environ)
    env["UPSMGR_CONFIG"] = cfg_path
    env["UPSMGR_DATA"] = work
    # 用临时端口：预写一份最小配置（端口取自配置里的 port 字段）
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"port": PORT, "password": PWD}, f)

    p = subprocess.Popen([sys.executable, SCRIPT], env=env,
                         stdout=open(log_path, "w"), stderr=subprocess.STDOUT)
    try:
        # 等端口起来
        ok = False
        for _ in range(40):
            time.sleep(0.25)
            try:
                r = urllib.request.urlopen("http://127.0.0.1:%d/api/login" % PORT, timeout=0.5)
                if r.status == 200:
                    ok = True
                    break
            except Exception:
                pass
        if not ok:
            print("[FAIL] 服务没起来")
            with open(log_path, encoding="utf-8", errors="replace") as f:
                print(f.read())
            return 1

        token = hashlib.sha256(("upsmgr:" + PWD).encode()).hexdigest()

        def get(path):
            req = urllib.request.Request(
                "http://127.0.0.1:%d%s" % (PORT, path),
                headers={"Cookie": "ups_token=" + token})
            return json.loads(urllib.request.urlopen(req, timeout=5).read())

        def post(path, payload):
            req = urllib.request.Request(
                "http://127.0.0.1:%d%s" % (PORT, path),
                data=json.dumps(payload).encode(),
                headers={"Cookie": "ups_token=" + token,
                         "Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=5).read())

        print("==== /api/status ====")
        s = get("/api/status")
        print("driver_kind = %s   driver_name = %s   version = %s"
              % (s.get("driver_kind"), s.get("driver_name"), s.get("version")))

        opts = s.get("driver_options")
        assert opts, "snapshot 缺 driver_options 字段"
        assert len(opts) == 4, "应有 4 个驱动选项，实际 %d" % len(opts)
        kinds = [o["kind"] for o in opts]
        assert kinds == ["auto", "nut", "hid", "mock"], "选项顺序错: %r" % kinds
        for o in opts:
            assert "label" in o and "available" in o and "active" in o, \
                "选项缺字段: %r" % o
        print("[PASS] driver_options 结构 OK：%s" % kinds)

        # 无硬件环境：应回落到 mock（有硬件时可能是 nut/hid，不做强断言）
        print("  当前生效驱动 = %s（有 UPS 的机器上可能是 nut/hid）" % s.get("driver_kind"))

        print("\n==== POST /api/settings (driver=hid) ====")
        r = post("/api/settings", {"driver": "hid"})
        assert r.get("ok"), "settings POST 失败: %r" % r
        assert "driver" in (r.get("changed") or []), "changed 应包含 driver"

        time.sleep(1)
        s2 = get("/api/status")
        assert s2.get("driver_kind") == "hid", \
            "切换后应 hid 生效，实际 %s" % s2.get("driver_kind")
        print("[PASS] 切换 driver=hid 后 rebuild 生效")

        print("\n==== POST /api/settings (driver_preferred=invalid) ====")
        r = post("/api/settings", {"driver_preferred": "xxx"})
        assert not r.get("ok"), "非法 preferred 应被拒: %r" % r
        print("[PASS] 非法 driver_preferred 值被拒绝")

        print("\n==== POST /api/settings (shutdown_batt_pct=150 越界夹取) ====")
        r = post("/api/settings", {"shutdown_batt_pct": 150})
        assert r.get("ok"), "越界值应被夹取而不是报错"
        assert get("/api/status")["cfg"]["shutdown_batt_pct"] == 100, "应夹到 100"
        print("[PASS] 越界阈值被夹到 100")

        print("\n==== 全部通过 ====")
        return 0
    finally:
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            pass
        print("\n临时目录（自动生成的配置与日志）:", work)


if __name__ == "__main__":
    sys.exit(main())
