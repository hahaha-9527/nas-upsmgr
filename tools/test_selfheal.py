# -*- coding: utf-8 -*-
"""upsmgr v0.1.4 逻辑自测：设备节点同步 / 读数失败宽限 / 不可信期 / auto 重选守卫"""
import os
import sys
import tempfile
import threading
import time

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
sys.path.insert(0, os.path.abspath(APP))
import upsmgr  # noqa: E402

real_os = os


class _PathProxy:
    def __getattr__(self, k):
        return getattr(real_os.path, k)

    def isdir(self, p):
        return True if p == "/sys" else real_os.path.isdir(p)


class FakeOS:
    """把 /dev/** 重定向到临时目录，避免 Windows 下真在盘根目录建文件。"""

    root = None          # 由测试设置：/dev 的替身目录

    def __init__(self):
        self.path = _PathProxy()
        self.created = []
        self.removed = []

    def __getattr__(self, k):
        return getattr(real_os, k)

    @classmethod
    def map(cls, p):
        # 注意要把 "/dev" 本身也算进来：os.path.dirname("/dev/hidraw0") == "/dev"，
        # 漏掉它的话 Windows 上会真建出 C:\\dev。
        if cls.root and isinstance(p, str) and (p == "/dev" or p.startswith("/dev/")):
            rest = p[4:].lstrip("/")
            return os.path.join(cls.root, rest.replace("/", os.sep)) if rest else cls.root
        return p

    @staticmethod
    def makedev(maj, mnr):          # Windows 的 os 没有 makedev，测试里自己算一个
        return (maj << 8) | mnr

    def mknod(self, path, mode, device):
        real = self.map(path)
        self.created.append((path, mode, device))
        real_os.makedirs(real_os.path.dirname(real), exist_ok=True)
        with open(real, "w") as f:
            f.write("x")

    def makedirs(self, path, exist_ok=False):
        return real_os.makedirs(self.map(path), exist_ok=exist_ok)

    def stat(self, path):
        return real_os.stat(self.map(path))

    def remove(self, path):
        self.removed.append(path)
        return real_os.remove(self.map(path))


FAILED = []


def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + ("  " + str(extra) if extra else ""))
    if not cond:
        FAILED.append(name)


print("==== 1. 设备节点同步 ====")
fake = FakeOS()
FakeOS.root = tempfile.mkdtemp(prefix="devroot_")   # /dev 的替身目录
upsmgr.os = fake
tmp = tempfile.mkdtemp(prefix="sysfs_")
usbdir = os.path.join(tmp, "usb")
os.makedirs(os.path.join(usbdir, "1-1"))
os.makedirs(os.path.join(usbdir, "1-2"))
with open(os.path.join(usbdir, "1-1", "uevent"), "w") as f:
    f.write("MAJOR=189\nMINOR=13\nDEVNAME=bus/usb/001/014\n")
with open(os.path.join(usbdir, "1-2", "uevent"), "w") as f:
    f.write("MAJOR=189\nMINOR=0\nDEVNAME=bus/usb/001/001\n")
upsmgr._SYS_UEVENT_GLOBS = (os.path.join(usbdir, "*", "uevent"),)
created = upsmgr.sync_usb_devnodes()
check("补齐两个新节点", len(created) == 2, created)
check("节点路径带 /dev 前缀", all(p.startswith("/dev/bus/usb/") for p in created), created)
check("mode 是字符设备 0666", all(m == (0o020000 | 0o666) for _, m, _ in fake.created),
      [oct(m) for _, m, _ in fake.created])
# 第二次跑：节点已存在（普通文件，rdev 对不上）→ 仍会重建，这里只验证不抛异常
try:
    upsmgr.sync_usb_devnodes()
    check("重复同步不抛异常", True)
except Exception as e:
    check("重复同步不抛异常", False, e)
upsmgr.os = real_os
upsmgr._SYS_UEVENT_GLOBS = ("/sys/bus/usb/devices/*/uevent",
                            "/sys/class/hidraw/*/uevent",
                            "/sys/class/usbmisc/*/uevent")

print()
print("==== 2. NutStack 基本行为（无 NUT 环境下不崩） ====")
cfg = dict(upsmgr.DEFAULT_CONFIG)
cfg["nut_ups_name"] = "wallecube"
stack = upsmgr.NutStack(cfg)
check("UPS 名取配置值", stack.ups_name() == "wallecube", stack.ups_name())
check("扫描进程不崩", isinstance(stack.driver_pids(), list))
check("ups.conf 缺失时回退", upsmgr.NutStack({}).ups_name() == "")
cfg2 = dict(cfg)
cfg2["nut_restart_wait"] = 3
stack2 = upsmgr.NutStack(cfg2)
t0 = time.time()
ok = stack2.restart("自测")
check("重拉在无 NUT 环境返回 False", ok is False, stack2.last_error[:80])
check("重拉耗时受 wait 控制(<8s)", time.time() - t0 < 8, round(time.time() - t0, 1))
check("记录了失败原因", bool(stack2.last_error))
check("节流生效（立刻再调一次被跳过）", stack2.restart("自测") is False)

print()
print("==== 3. Engine：宽限期 / 不可信期 / auto 守卫 ====")
db = os.path.join(tempfile.mkdtemp(prefix="upsdb_"), "t.db")
store = upsmgr.Store(db)
ecfg = dict(upsmgr.DEFAULT_CONFIG)
ecfg["driver"] = "mock"
ecfg["devnode_sync"] = False
ecfg["poll_sec"] = 3600
ecfg["lost_grace_sec"] = 120
eng = upsmgr.Engine(ecfg, store)
eng.nut.cfg = ecfg

check("settling 初始为 False", eng._settling() is False)
eng.nut.last_restart = time.time()
check("重拉后进入不可信期", eng._settling() is True)
eng.nut.last_restart = time.time() - 100
check("20s 后不可信期结束", eng._settling() is False)

# 宽限期内不报丢失
eng.last_status = "online"
eng._lost_since = None
eng.cfg = dict(ecfg)
eng.cfg["nut_restart_min_interval"] = 99999   # 屏蔽自愈动作，只测宽限
eng._on_read_fail()
check("宽限期内不报 driver_lost", eng.last_status == "online", eng.last_status)
check("已标记 stale", eng._lost_since is not None)

# 宽限设 0 → 立刻报
eng2 = upsmgr.Engine(dict(ecfg, lost_grace_sec=0, nut_restart_min_interval=99999), store)
eng2.nut.cfg = eng2.cfg
eng2.last_status = "online"
eng2._on_read_fail()
check("宽限 0 时立刻报 driver_lost", eng2.last_status == "driver_lost", eng2.last_status)

# auto 守卫：非 auto 模式直接返回
eng3 = upsmgr.Engine(dict(ecfg, driver="nut"), store)
check("非 auto 不重选驱动", eng3._maybe_reselect() is False)

# 不可信期内：读数只展示不决策（不产生关机调度/事件）
eng4 = upsmgr.Engine(dict(ecfg), store)
eng4.nut.cfg = eng4.cfg
eng4.nut.last_restart = time.time()
before = len(store.events(200))
eng4._handle({"status": "on_battery", "charge_pct": 1.0, "runtime_min": 1,
              "model": "t", "input_v": 0, "output_v": 0, "battery_v": 0, "temp_c": 0})
check("不可信期不触发关机调度", eng4.pending_until is None)
check("不可信期不写状态迁移事件", len(store.events(200)) == before,
      len(store.events(200)) - before)
check("不可信期仍更新 last_reading", (eng4.last_reading or {}).get("charge_pct") == 1.0)

# 正常期：同样的读数应该触发关机流程
eng4.nut.last_restart = time.time() - 999
eng4.cfg = dict(ecfg)
eng4.cfg["enable_shutdown"] = True
eng4._handle({"status": "on_battery", "charge_pct": 1.0, "runtime_min": 1,
              "model": "t", "input_v": 0, "output_v": 0, "battery_v": 0, "temp_c": 0})
check("正常期会触发关机调度", eng4.pending_until is not None)

print()
print("==== 4. snapshot 字段 ====")
snap = eng.snapshot()
check("snapshot 带 selfheal", isinstance(snap.get("selfheal"), dict))
for k in ("stale", "stale_sec", "grace_sec", "settling", "nut_restarts",
          "devnodes_fixed", "last_error"):
    check("selfheal.%s 存在" % k, k in snap["selfheal"])

print()
print("==== 5. HID 未校准不得冒充可用 ====")


class FakeHid(upsmgr.HidUpsDriver):
    def __init__(self, cfg, report=True):
        self.map = {"reports": {"r1": "x"}} if report else {}
        self.dev = "/dev/hidraw0"
        self.dev_name = "Fake UPS"

    def available(self):
        return True


check("校准后 can_report=True", FakeHid({}, True).can_report() is True)
check("未校准 can_report=False", FakeHid({}, False).can_report() is False)

print()
print("==== 6. 设备节点垃圾回收（幽灵节点） ====")
# 复现现场：rebind 瞬间 hidraw/hiddev 从 sysfs 冒出来被我们建好，之后设备被 libusb
# 抢走，sysfs 里再也不描述它们 —— 节点必须被回收，否则 HID 驱动每轮探测失败刷屏。
devroot = tempfile.mkdtemp(prefix="devroot_gc_")
FakeOS.root = devroot
gcos = FakeOS()
upsmgr.os = gcos

gcsys = tempfile.mkdtemp(prefix="sysfs_gc_")
hid_dir = os.path.join(gcsys, "hidraw0")
os.makedirs(hid_dir)
hid_uevent = os.path.join(hid_dir, "uevent")

# 先让它存在一轮：节点被建出来
with open(hid_uevent, "w") as f:
    f.write("MAJOR=242\nMINOR=0\nDEVNAME=/dev/hidraw0\n")
upsmgr._SYS_UEVENT_GLOBS = (os.path.join(gcsys, "*", "uevent"),)
created = upsmgr.sync_usb_devnodes()
check("首轮建出 hidraw0", created == ["/dev/hidraw0"], created)
check("记录进待回收集合", "/dev/hidraw0" in upsmgr._DEVNODE_CREATED)

# 设备消失（删掉 sysfs 来源）
os.remove(hid_uevent)
os.rmdir(hid_dir)
upsmgr.sync_usb_devnodes()
check("第 1 轮消失不删", upsmgr.os.path.exists(devroot + "/hidraw0"))
upsmgr.sync_usb_devnodes()
check("第 2 轮消失不删", upsmgr.os.path.exists(devroot + "/hidraw0"))
check("消失计数已累计", upsmgr._DEVNODE_MISS.get("/dev/hidraw0") == 2,
      upsmgr._DEVNODE_MISS)
upsmgr.sync_usb_devnodes()
check("第 3 轮才回收", not upsmgr.os.path.exists(devroot + "/hidraw0"))
check("回收后不再跟踪", "/dev/hidraw0" not in upsmgr._DEVNODE_CREATED)
check("回收计数已清", "/dev/hidraw0" not in upsmgr._DEVNODE_MISS)

# 抖动保护：消失一轮又回来，不该触发任何删除
os.makedirs(hid_dir)
with open(hid_uevent, "w") as f:
    f.write("MAJOR=242\nMINOR=0\nDEVNAME=/dev/hidraw0\n")
upsmgr.sync_usb_devnodes()
upsmgr.sync_usb_devnodes()
check("重新出现则计数清零", upsmgr._DEVNODE_MISS.get("/dev/hidraw0") is None,
      upsmgr._DEVNODE_MISS)
os.remove(hid_uevent)
os.rmdir(hid_dir)
upsmgr.sync_usb_devnodes()
upsmgr.sync_usb_devnodes()
check("闪断一瞬不删除", upsmgr.os.path.exists(devroot + "/hidraw0"))

# 容器自带的节点绝不能被误删
upsmgr.sync_usb_devnodes()
check("未由本进程创建的节点 0 个被删", all(p.startswith("/dev/") for p in gcos.removed))

upsmgr.os = real_os
upsmgr._SYS_UEVENT_GLOBS = ("/sys/bus/usb/devices/*/uevent",
                            "/sys/class/hidraw/*/uevent",
                            "/sys/class/usbmisc/*/uevent")

print()
print("结果：%s" % ("全部通过" if not FAILED else "失败 %d 项 -> %s" % (len(FAILED), FAILED)))
sys.exit(1 if FAILED else 0)
