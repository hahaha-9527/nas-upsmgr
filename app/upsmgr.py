# -*- coding: utf-8 -*-
"""电源管理 (upsmgr) —— NAS UPS 电源管理守护进程 + 内嵌 Web 面板

与硬件 UPS 通讯的 NAS 应用：
  - 驱动抽象：hid（USB HID UPS，探测 + 联调校准） / mock（模拟 UPS，可人工制造断电事件）
  - SQLite 落库：读数历史 + 事件日志（默认保留 7 天）
  - 断电策略引擎：低电量 / 延迟阈值 -> 预关机倒计时 -> 执行关机命令
    （enable_shutdown 默认 false；dry_run 默认 true，只打日志不真关）
  - 内嵌 Web 面板：状态卡 / 历史 / 事件 / 设置 / 模拟控制台，口令闸门

只依赖 Python 3 标准库。
"""
import base64
import glob
import hashlib
import html
import json
import os
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:                       # Linux 专属；Windows 本地开发兜底
    import fcntl
except ImportError:
    fcntl = None

APP_VERSION = "v0.1.3"
APP_NAME = "电源管理"

DEFAULT_CONFIG = {
    "port": 9750,
    "driver": "auto",            # auto | hid | nut | mock
    "driver_preferred": "nut",   # auto 模式下两者都识别时的优先级：hid | nut（默认 nut：NUT 字段更标准更全）
    "poll_sec": 10,
    "low_batt_pct": 50,          # 电量低于此值 -> 事件提醒
    "shutdown_batt_pct": 30,     # 电量低于此值 -> 触发关机流程
    "shutdown_mode": "battery",  # 关机等待方式：battery=直到电量不足 | timer=自定义时间（断电 N 分钟后）
    "shutdown_delay_min": 5,     # 市电中断后，无低电触发时的最长等待（分钟）
    "enable_shutdown": False,    # 总开关：是否允许执行关机（默认关）
    "dry_run": True,             # 兼容字段（已废弃，策略不再引用）
    "shutdown_command": "/sbin/shutdown -h now",   # 容器内路径；/host 存在时自动加 chroot
    "pre_shutdown_commands": [], # 关机前逐条执行（如停容器）
    "password": "admin",         # 面板口令（首装默认，务必在面板里改）
    "history_days": 7,
    "notify_enabled": True,      # 关键事件同步到铁牛通知栏（容器内 /host/userdata/db 可见时生效）
    "notify_uid": 1000,          # 通知目标用户 uid（Feige=1000）
}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def now_ts():
    return int(time.time())


def ts_str(ts):
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


# ============================================================ 铁牛通知栏推送
# 级别 tag 取自系统内嵌字典（go-i18n JSON，filemanage 二进制内）：Info/Alert/Error/Critical
_TN_LEVELS = {"info": "common.msgcenter.levels.Info",
              "warn": "common.msgcenter.levels.Alert",
              "bad": "common.msgcenter.levels.Critical"}
# 借壳系统模板：serviceName=Notice（标题"系统通知"）+ Test.TestMsg（"测试消息{{.Desc}}"，
# Desc 是全字典唯一自由文本参数）→ 正文 = "测试消息" + 自由文案，无英文前缀垃圾
_TN_SERVICE = "common.msgcenter.services.Notice"
_TN_MESSAGE = "common.msgcenter.messages.Test.TestMsg"


def tieniu_push(level, text):
    """关键事件写入铁牛消息中心（msgcenter.db）。失败只记日志，不影响主流程。

    依赖 tpk compose 的 /:/host 挂载；/host/userdata/db/msgcenter.db 不存在即跳过
    （本地开发 / 未挂载环境）。与系统服务同款协议：flock msgcenter_write.lock 后
    写 msgcenter_msg_records + msgcenter_user_messages。
    显示格式（已与用户定稿）：来源"系统通知"、正文"测试消息<文案>"，级别按事件映射
    Info/Alert/Critical。
    """
    db = os.environ.get("TIENIU_MSGDB", "/host/userdata/db/msgcenter.db")
    if not os.path.exists(db):
        return False
    try:
        uid = int(os.environ.get("UPSMGR_NOTIFY_UID", "1000"))
        lk = open(os.path.join(os.path.dirname(db), "msgcenter_write.lock"), "a+")
        try:
            if fcntl:
                fcntl.flock(lk, fcntl.LOCK_EX)
            c = sqlite3.connect(db, timeout=10)
            try:
                now = time.strftime("%Y-%m-%d %H:%M:%S.") + "%03d+08:00" % (int(time.time() * 1000) % 1000)
                cur = c.execute(
                    "INSERT INTO msgcenter_msg_records (session_id,service_name,level,created_at,"
                    "message,msg_params,msg_type,has_broadcast,user_id,expired_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("", _TN_SERVICE, _TN_LEVELS.get(level, _TN_LEVELS["info"]), now,
                     _TN_MESSAGE, json.dumps({"Desc": text}, ensure_ascii=False), "", 0, uid,
                     "0001-01-01 00:00:00+00:00"))
                c.execute("INSERT INTO msgcenter_user_messages (user_id,msg_id,has_sent) VALUES (?,?,0)",
                          (uid, cur.lastrowid))
                c.commit()
            finally:
                c.close()
        finally:
            if fcntl:
                fcntl.flock(lk, fcntl.LOCK_UN)
            lk.close()
        log("notify: -> 铁牛通知栏 (%s) %s" % (level, text))
        return True
    except Exception as e:
        log("notify: 写通知栏失败 %r" % e)
        return False


# ============================================================ 模拟驱动
class MockDriver:
    """模拟 UPS：无硬件时兜底，也是策略引擎的联调工具。

    通过 /api/mock 可以人工制造事件：
      power_off / power_on / charge_set(百分比) / load_set(百分比)
    """

    name = "模拟 UPS (Mock)"
    kind = "mock"

    def __init__(self, cfg):
        self.lock = threading.RLock()   # 可重入：mock() 持锁时会再调 read()
        self.online = True
        self.charge = 100.0
        self.load = 32.0
        self.input_v = 220.0
        self.drain_per_min = 1.2     # 电池供电时每分钟掉的电量百分比
        self.model = "SimUPS-1000"

    def _tick(self, dt_sec):
        dt_min = dt_sec / 60.0
        if self.online:
            self.charge = min(100.0, self.charge + 4.0 * dt_min)
            self.input_v = round(220 + (time.time() % 7) * 0.6, 1)
        else:
            self.charge = max(0.0, self.charge - self.drain_per_min * dt_min)
            self.input_v = 0.0
        self.load = max(5.0, min(100.0, self.load + (0.4 if int(time.time()) % 17 == 0 else 0.0)))

    def read(self):
        with self.lock:
            dt = getattr(self, "_last", None)
            t = time.time()
            if dt is not None:
                self._tick(min(60.0, t - dt))
            self._last = t
            status = "online" if self.online else "on_battery"
            runtime = 999 if self.online else round(self.charge / self.drain_per_min)
            return {
                "status": status,
                "charge_pct": round(self.charge, 1),
                "load_pct": round(self.load, 1),
                "runtime_min": runtime,
                "input_v": self.input_v,
                "output_v": 220.0,
                "battery_v": round(12.0 + self.charge * 0.014, 2),
                "temp_c": 31.0,
                "model": self.model,
                "detail": "模拟数据",
            }

    def mock(self, action, value=None):
        with self.lock:
            if action == "power_off":
                self.online = False
            elif action == "power_on":
                self.online = True
            elif action == "charge_set":
                self.charge = max(0.0, min(100.0, float(value)))
            elif action == "load_set":
                self.load = max(5.0, min(100.0, float(value)))
            elif action == "drain_set":
                self.drain_per_min = max(0.1, min(20.0, float(value)))
            return self.read()

    def available(self):
        return True     # 模拟驱动永远兜底可用（开发联调场景）


# ============================================================ HID 驱动（探测 + 联调工具）
_HIDIOCGRDESC = 0x90044802   # _IOR('H', 0x02, struct hidraw_report_descriptor)


class HidUpsDriver:
    """USB HID UPS 驱动（联调版）。

    目前实现：
      1. 扫描 /dev/hidraw*，用 HID 报告描述符识别电池系统用法页 0x84；
      2. 打开设备，周期性抓取 feature/input 报告并落日志 —— 供真机联调时
         校准字段（报告 id -> 含义）；
      3. 校准完成后把字段映射写进 /data/hid_map.json，read() 即可产出真实读数。

    在校准前 read() 返回 detected 状态与原始信息，面板会明确提示"待联调"。
    """

    name = "USB HID UPS"
    kind = "hid"

    def __init__(self, cfg):
        self.cfg = cfg
        self.dev = None
        self.dev_name = ""
        self.desc_hex = ""
        self.map = {}
        self.map_path = os.path.join(cfg.get("data_dir", "/data"), "hid_map.json")
        self._load_map()
        self._probe()

    def _load_map(self):
        try:
            with open(self.map_path, "r", encoding="utf-8") as f:
                self.map = json.load(f)
        except Exception:
            self.map = {}

    def _save_map(self):
        try:
            with open(self.map_path, "w", encoding="utf-8") as f:
                json.dump(self.map, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log("hid: save map failed: %s" % e)

    @staticmethod
    def _has_battery_usage_page(desc):
        """描述符里出现 Global Usage Page = 0x84（Battery System）即视为 UPS。"""
        i, n = 0, len(desc)
        while i < n:
            b = desc[i]
            size = b & 0x03
            if size == 3:
                size = 4
            if b == 0x04 and i + 1 < n:      # Usage Page (1 字节)
                if desc[i + 1] == 0x84:
                    return True
            i += 1 + size
        return False

    def _probe(self):
        for path in sorted(glob.glob("/dev/hidraw*")):
            n = os.path.basename(path)
            name = ""
            try:
                with open("/sys/class/hidraw/%s/device/uevent" % n, "r") as f:
                    for line in f:
                        if line.startswith("HID_NAME="):
                            name = line.split("=", 1)[1].strip()
            except Exception:
                pass
            desc = b""
            if fcntl is not None:
                try:
                    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
                    try:
                        buf = bytearray(4 + 4096)
                        fcntl.ioctl(fd, _HIDIOCGRDESC, buf, True)
                        dlen = struct.unpack_from("<I", buf, 0)[0]
                        desc = bytes(buf[4:4 + min(dlen, 4096)])
                    finally:
                        os.close(fd)
                except Exception as e:
                    log("hid: %s read desc failed: %s" % (path, e))
            if (self._has_battery_usage_page(desc)
                    or any(k in name.lower() for k in ("ups", "power", "battery"))):
                self.dev, self.dev_name, self.desc_hex = path, name, desc.hex()
                log("hid: UPS detected at %s (%s)" % (path, name))
                return
        self.dev = None
        log("hid: no HID UPS found")

    def available(self):
        if self.dev and os.path.exists(self.dev):
            return True
        self._probe()
        return self.dev is not None

    def read(self):
        if not self.available():
            return None
        out = {
            "status": "detected",
            "charge_pct": None, "load_pct": None, "runtime_min": None,
            "input_v": None, "output_v": None, "battery_v": None, "temp_c": None,
            "model": self.dev_name or "HID UPS",
            "detail": "已识别 USB 设备，等待联调校准（原始报告见日志）",
        }
        # 联调：周期性抓 feature 报告 1..8，落日志供字段校准
        if fcntl is not None:
            try:
                fd = os.open(self.dev, os.O_RDWR | os.O_NONBLOCK)
                try:
                    for rid in range(1, 9):
                        buf = bytearray(1 + 64)
                        buf[0] = rid
                        try:
                            n = fcntl.ioctl(fd, 0xC0484803, buf, True)  # HIDIOCGFEATURE(64)
                            data = bytes(buf[:n])
                            key = "r%d" % rid
                            if key in self.map.get("reports", {}):
                                continue
                            if data[1:].strip(b"\x00"):
                                log("hid: feature report %d = %s" % (rid, data.hex()))
                        except OSError:
                            continue
                finally:
                    os.close(fd)
            except Exception as e:
                out["detail"] = "读取失败: %s" % e
        return out


# ============================================================ NUT 桥接驱动
class NutBridgeDriver:
    """NUT (Network UPS Tools) 桥接驱动。

    调 upsc 命令取 NUT 已识别的 UPS 字段，统一映射到 UPSReading 结构。
    适配 NUT 支持的数百款 UPS；不依赖 Python NUT 绑定，只用命令行。
    依赖容器内已安装 nut-client（apt-get install nut-client）且 upsd 监听 127.0.0.1:3493。
    """

    name = "NUT 桥接"
    kind = "nut"

    def __init__(self, cfg):
        self.cfg = cfg
        self._ups_name = None          # upsc -l 探出来的 UPS 名字
        self._ups_host = "127.0.0.1"
        self._ups_port = 3493
        self._cache = None            # 5s 内复用上次读数（避免轮询压垮 upsd）
        self._cache_t = 0.0
        self._probe_t = 0.0           # 上次 probe 时间（30s 节流）
        self._probe_done = False
        self._probe()

    def _probe(self):
        """探测 NUT 服务并取出第一个 UPS 名字。"""
        if self._probe_done and time.time() - self._probe_t < 30:
            return
        self._probe_t = time.time()
        try:
            p = subprocess.run(
                ["upsc", "-l", "%s:%d" % (self._ups_host, self._ups_port)],
                capture_output=True, text=True, timeout=3)
            if p.returncode == 0:
                lines = [l.strip() for l in p.stdout.splitlines() if l.strip()]
                if lines:
                    self._ups_name = lines[0]
                    self._probe_done = True
                    log("nut: UPS detected: %s" % self._ups_name)
                    return
        except FileNotFoundError:
            log("nut: upsc 命令不存在（容器没装 nut-client）")
        except Exception as e:
            log("nut: probe failed: %s" % e)
        self._probe_done = True

    def available(self):
        if not self._ups_name:
            self._probe()
            return self._ups_name is not None
        # 已有 UPS 名，验证一次能否取到字段
        raw = self._read_raw()
        return bool(raw)

    def _read_raw(self):
        """调 upsc 取原始键值对字典。"""
        if not self._ups_name:
            return None
        target = "%s@%s:%d" % (self._ups_name, self._ups_host, self._ups_port)
        try:
            p = subprocess.run(
                ["upsc", target],
                capture_output=True, text=True, timeout=4)
        except Exception as e:
            log("nut: read failed: %s" % e)
            return None
        if p.returncode != 0:
            return None
        out = {}
        for line in p.stdout.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                out[k.strip()] = v.strip()
        return out

    def read(self):
        # 5s 缓存
        if self._cache and time.time() - self._cache_t < 5:
            return self._cache
        raw = self._read_raw()
        if not raw:
            return None
        # ups.status 多 token：OL/OB/LB/RB/CHRG/DISCHRG/BYPASS/OFF/OVER/FSD 等
        st_str = raw.get("ups.status", "OL")
        tokens = st_str.split()
        if "FSD" in tokens:
            status = "shutdown_imminent"
        elif "OB" in tokens:
            status = "on_battery"
        elif "OFF" in tokens:
            status = "offline"
        elif "OL" in tokens:
            status = "online"
        else:
            status = "unknown"
        if "LB" in tokens and status != "on_battery":
            status = "low_battery"
        # ALARM 不改主状态（OL 时仍是 online），但 detail 要标出来

        def _f(key, cast=float, d=None):
            v = raw.get(key)
            if v is None or v == "":
                return d
            try:
                return cast(v)
            except Exception:
                return d

        # battery.runtime: NUT 在市电供电时给占位值 65534/32767 表示"无限大"，
        # 跟 MockDriver 用 999 表示"市电供电" 一致，避免面板显示 1092 分钟误导
        rt = raw.get("battery.runtime")
        runtime_min = None
        if rt:
            try:
                rt_i = int(float(rt))
                if rt_i >= 32000:           # NUT 占位值（无限大）
                    runtime_min = 999
                else:
                    runtime_min = max(0, rt_i // 60)
            except Exception:
                pass

        # temp_c: 优先 ups.temperature，WalleCube 用 battery.temperature（fallback）
        temp_c = _f("ups.temperature")
        if temp_c is None:
            temp_c = _f("battery.temperature")

        # output_w: 部分 UPS（如 WalleCube openUPS HID）不上报 ups.load，
        # 用输出电压×电流推算实际输出功率，供面板在无负载百分比时显示
        _ov = _f("output.voltage")
        _oc = _f("output.current")
        output_w = round(_ov * _oc, 1) if (_ov is not None and _oc is not None) else None

        r = {
            "status": status,
            "charge_pct": _f("battery.charge"),
            "load_pct": _f("ups.load"),
            "output_w": output_w,
            "runtime_min": runtime_min,
            "input_v": _f("input.voltage"),
            "output_v": _f("output.voltage"),
            "battery_v": _f("battery.voltage"),
            "temp_c": temp_c,
            "model": raw.get("ups.model") or raw.get("device.model") or "NUT UPS",
            "mfr": raw.get("ups.mfr") or raw.get("device.mfr") or "",
            "detail": "ups.status=%s  alarm=%s  (mfr=%s model=%s)" % (
                st_str, raw.get("ups.alarm", "无"),
                raw.get("ups.mfr", "?"), raw.get("ups.model", "?")),
        }
        self._cache = r
        self._cache_t = time.time()
        return r


def make_driver(cfg):
    """根据 cfg['driver'] 选择驱动。

    auto 模式：先探测 NUT + HID 两者可用性，按 cfg['driver_preferred'] 决定生效；
              都不可用 -> 回 mock（开发兜底，避免无 UPS 时面板报死）。
    显式指定：nut -> NutBridgeDriver；hid -> HidUpsDriver；mock -> MockDriver。
    """
    pref = (cfg.get("driver") or "auto").lower()
    preferred = (cfg.get("driver_preferred") or "nut").lower()

    if pref == "mock":
        return MockDriver(cfg)
    if pref == "nut":
        return NutBridgeDriver(cfg)
    if pref == "hid":
        return HidUpsDriver(cfg)
    # auto
    nut_drv = NutBridgeDriver(cfg)
    hid_drv = HidUpsDriver(cfg)
    nut_ok = nut_drv.available()
    hid_ok = hid_drv.available()
    if nut_ok and hid_ok:
        return hid_drv if preferred == "hid" else nut_drv
    if nut_ok:
        return nut_drv
    if hid_ok:
        return hid_drv
    log("driver: auto 模式下 NUT 与 HID 都不可用，回落 mock（仅监控）")
    return MockDriver(cfg)


# ============================================================ 存储
class Store:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        with self.lock:
            c = self.db.cursor()
            c.execute("""CREATE TABLE IF NOT EXISTS readings(
                ts INTEGER PRIMARY KEY, status TEXT, charge REAL, load REAL,
                runtime INTEGER, input_v REAL, battery_v REAL, temp REAL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS events(
                ts INTEGER, level TEXT, type TEXT, message TEXT)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_readings ON readings(ts)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_events ON events(ts)")
            try:
                c.execute("ALTER TABLE readings ADD COLUMN output_w REAL")
            except sqlite3.OperationalError:
                pass  # 列已存在
            self.db.commit()

    def add_reading(self, r):
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO readings"
                "(ts,status,charge,load,runtime,input_v,battery_v,temp,output_w) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (now_ts(), r.get("status"), r.get("charge_pct"), r.get("load_pct"),
                 r.get("runtime_min"), r.get("input_v"), r.get("battery_v"),
                 r.get("temp_c"), r.get("output_w")))
            self.db.commit()

    def add_event(self, level, etype, message):
        ts = now_ts()
        with self.lock:
            self.db.execute("INSERT INTO events VALUES(?,?,?,?)", (ts, level, etype, message))
            self.db.commit()
        log("[%s] %s: %s" % (level, etype, message))
        return ts

    def history(self, minutes=360, max_points=320):
        since = now_ts() - minutes * 60
        with self.lock:
            rows = self.db.execute(
                "SELECT ts,status,charge,load,runtime,input_v,battery_v,output_w FROM readings "
                "WHERE ts>=? ORDER BY ts", (since,)).fetchall()
        if len(rows) <= max_points:
            return rows
        step = len(rows) / float(max_points)
        return [rows[int(i * step)] for i in range(max_points)]

    def events(self, limit=100):
        with self.lock:
            rows = self.db.execute(
                "SELECT ts,level,type,message FROM events ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        return rows

    def prune(self, days):
        cutoff = now_ts() - days * 86400
        with self.lock:
            self.db.execute("DELETE FROM readings WHERE ts<?", (cutoff,))
            self.db.execute("DELETE FROM events WHERE ts<?", (cutoff,))
            self.db.commit()


# ============================================================ 策略引擎
class Engine:
    """轮询驱动 -> 落库 -> 状态迁移事件 -> 断电关机决策。

    关机决策（三者任一满足即进入预关机倒计时）：
      1. 市电中断 且 电量 <= shutdown_batt_pct
      2. 市电中断 持续 >= shutdown_delay_min 分钟
      3. 市电中断 且 预计续航 <= shutdown_delay_min 分钟
    倒计时 60 秒（cancel 窗口）后执行：pre_shutdown_commands -> shutdown_command。
    enable_shutdown=False 或 dry_run=True 时只记录不执行。
    """

    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        self.driver = make_driver(cfg)
        self.state_lock = threading.Lock()
        self.last_status = None
        self.on_battery_since = None
        self.pending_until = None      # 预关机执行时间戳
        self.pending_reason = ""
        self.last_reading = None
        self._stop = False
        self._driver_opts_cache = None   # 驱动选择器面板数据缓存（30s 节流）
        self._driver_opts_t = 0.0
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    # ---- 配置热更新 / 模拟动作 ----
    def reload_config(self, cfg):
        with self.state_lock:
            self.cfg = cfg

    def rebuild_driver(self):
        """配置改了 driver/driver_preferred，重建驱动实例。轮询循环会自动用新驱动。"""
        with self.state_lock:
            old = self.driver
            try:
                close = getattr(old, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
            self.driver = make_driver(self.cfg)
            self.last_status = None
            self.on_battery_since = None
            self.pending_until = None
            self._driver_opts_cache = None
        log("engine: driver rebuilt -> %s (kind=%s)" % (
            getattr(self.driver, "name", "?"), getattr(self.driver, "kind", "?")))

    def mock(self, action, value=None):
        if isinstance(self.driver, MockDriver):
            r = self.driver.mock(action, value)
            return r
        return None

    # ---- 主循环 ----
    def _loop(self):
        prune_t = 0.0
        while not self._stop:
            poll = max(3, int(self.cfg.get("poll_sec", 10)))
            try:
                r = self.driver.read()
                if r is None:
                    if self.last_status not in (None, "driver_lost"):
                        self.store.add_event("warn", "driver_lost", "UPS 通讯丢失（设备消失或读取失败）")
                        self.last_status = "driver_lost"
                else:
                    self._handle(r)
            except Exception as e:
                log("engine: %r" % e)
            if time.time() - prune_t > 3600:
                try:
                    self.store.prune(int(self.cfg.get("history_days", 7)))
                except Exception:
                    pass
                prune_t = time.time()
            self._check_pending()
            time.sleep(poll)

    def _handle(self, r):
        st = r.get("status")
        with self.state_lock:
            cfg = dict(self.cfg)
        prev = self.last_status
        self.last_reading = r
        if st in ("online", "on_battery", "detected",
                  "low_battery", "shutdown_imminent"):
            self.store.add_reading(r)

        if st != prev:
            if st == "shutdown_imminent":
                self.store.add_event("bad", "ups_fsd", "UPS 报告即将断电（FSD）")
            if st == "on_battery":
                self.on_battery_since = now_ts()
                self.store.add_event("warn", "power_lost", "市电中断，已切换电池供电（电量 %s%%）" % r.get("charge_pct"))
                if cfg.get("notify_enabled", True):
                    tieniu_push("warn", "市电中断，NAS 已切换电池供电（剩余 %s%%）" % r.get("charge_pct"))
            elif prev == "on_battery" and st == "online":
                self.on_battery_since = None
                if self.pending_until:
                    self.pending_until = None
                    self.store.add_event("info", "shutdown_cancelled", "市电恢复，已取消预关机")
                self.store.add_event("info", "power_restore", "市电恢复，充电中（电量 %s%%）" % r.get("charge_pct"))
                if cfg.get("notify_enabled", True):
                    tieniu_push("info", "市电已恢复，NAS 转回市电供电（电量 %s%%）" % r.get("charge_pct"))
            elif st == "detected" and prev is None:
                self.store.add_event("info", "driver_detected",
                                     "已识别 UPS 设备：%s（待联调校准）" % r.get("model", ""))
            self.last_status = st

        # FSD：UPS 已经宣布"马上断电"，与电量/已断电时长都无关，直接进预关机窗口
        if st == "shutdown_imminent":
            if not self.pending_until:
                self._schedule_shutdown("UPS 报告即将断电（FSD）", cfg)
            return

        if st != "on_battery":
            return

        # charge_pct 必须允许为 None：NUT 的 battery.charge 读不到时 _f() 回 None，
        # HID 驱动更是整组字段都可能是 None。老写法 `or 0` 会把"读不到电量"
        # 变成"电量 0%"，于是一次读取抖动就能触发关机流程。
        charge = r.get("charge_pct")
        runtime = r.get("runtime_min")
        has_charge = isinstance(charge, (int, float))
        low, crit = cfg.get("low_batt_pct", 50), cfg.get("shutdown_batt_pct", 30)
        delay = cfg.get("shutdown_delay_min", 5)
        since = self.on_battery_since or now_ts()
        held_min = (now_ts() - since) / 60.0

        if not has_charge and not self._flagged("nocharge"):
            self._flag_set("nocharge")
            self.store.add_event("warn", "charge_missing",
                                 "UPS 未上报电量，基于电量的关机判据暂不生效（仅按时间/续航判断）")
            if cfg.get("notify_enabled", True):
                tieniu_push("warn", "UPS 未上报电量，暂时无法按电量判断是否需要关机")
        if has_charge:
            self._flag_clear("nocharge")

        if has_charge and charge <= crit and not self._flagged("crit"):
            self._flag_set("crit")
            self.store.add_event("bad", "battery_critical", "电量严重不足（%s%%），触发关机流程" % charge)
            if cfg.get("notify_enabled", True):
                tieniu_push("bad", "UPS 电量严重不足（%s%%），即将触发 NAS 关机流程" % charge)
        elif has_charge and charge <= low and not self._flagged("low"):
            self._flag_set("low")
            self.store.add_event("warn", "battery_low", "电量偏低（%s%%）" % charge)
            if cfg.get("notify_enabled", True):
                tieniu_push("warn", "UPS 电量偏低（%s%%），请留意市电" % charge)
        if has_charge and charge > low:
            self._flag_clear("low")
        if has_charge and charge > crit:
            self._flag_clear("crit")

        if self.pending_until:
            return
        trig = None
        mode = cfg.get("shutdown_mode", "battery")
        if mode == "timer":
            if held_min >= delay:
                trig = "市电中断已持续 %d 分钟（阈值 %d 分钟）" % (held_min, delay)
        else:  # battery：直到电量不足
            if has_charge and charge <= crit:
                trig = "电量 %s%% 低于关机阈值 %s%%" % (charge, crit)
            elif isinstance(runtime, (int, float)) and runtime <= delay:
                trig = "预计续航 %s 分钟 低于 %d 分钟" % (runtime, delay)
        if trig:
            self._schedule_shutdown(trig, cfg)

    def _schedule_shutdown(self, reason, cfg=None):
        """进入 60 秒预关机窗口（面板可取消）。"""
        if cfg is None:
            with self.state_lock:
                cfg = dict(self.cfg)
        self.pending_until = now_ts() + 60
        self.pending_reason = reason
        self.store.add_event("bad", "shutdown_scheduled",
                             "满足关机条件：%s。60 秒后执行关机（可在面板取消）" % reason)
        if cfg.get("notify_enabled", True):
            tieniu_push("bad", "UPS：将在 60 秒后关机（%s），可在面板取消" % reason)

    def _will_exec(self):
        with self.state_lock:
            cfg = dict(self.cfg)
        return bool(cfg.get("enable_shutdown"))

    def _flagged(self, k):
        return getattr(self, "_f_" + k, False)

    def _flag_set(self, k):
        setattr(self, "_f_" + k, True)

    def _flag_clear(self, k):
        setattr(self, "_f_" + k, False)

    def _check_pending(self):
        with self.state_lock:
            cfg = dict(self.cfg)
            until = self.pending_until
        if not until or now_ts() < until:
            return
        self.pending_until = None
        will_exec = bool(cfg.get("enable_shutdown"))
        cmds = list(cfg.get("pre_shutdown_commands", []))
        cmds.append(cfg.get("shutdown_command", "/sbin/shutdown -h now"))
        for c in cmds:
            line = "[EXECUTE] %s" % c
            if will_exec:
                ok, out = self._run_cmd(c)
                line += " -> %s" % ("ok" if ok else "FAIL: %s" % out)
            self.store.add_event("bad" if will_exec else "warn", "shutdown_exec", line)
        if not will_exec:
            self.store.add_event("warn", "shutdown_skipped",
                                 "已满足关机条件但「启用 UPS 支持」开关为关，未执行关机")

    def _run_cmd(self, cmd):
        # 容器里执行 shutdown 只会停掉容器自己：只要挂了宿主根（/host）就 chroot 进去跑。
        # 老写法额外要求命令"不以 / 开头"，而默认的 shutdown_command 恰好是
        # /sbin/shutdown —— 于是注释里说的"自动加 chroot"从来没生效过。
        full = ("chroot /host %s" % cmd) if os.path.isdir("/host") else cmd
        try:
            import subprocess
            p = subprocess.run(full, shell=True, capture_output=True, text=True, timeout=60)
            return p.returncode == 0, (p.stderr or p.stdout).strip()[:300]
        except Exception as e:
            return False, str(e)

    # ---- 状态给面板 ----
    def snapshot(self):
        with self.state_lock:
            cfg = dict(self.cfg)
        r = self.last_reading
        return {
            "driver_kind": getattr(self.driver, "kind", "?"),
            "driver_name": getattr(self.driver, "name", "?"),
            "driver_mode": (cfg.get("driver") or "auto").lower(),
            "driver_preferred": (cfg.get("driver_preferred") or "nut").lower(),
            "driver_options": self._driver_options(),
            "connected": self.last_status not in (None, "driver_lost"),
            "status": self.last_status,
            "reading": r,
            "on_battery_since": self.on_battery_since,
            "pending_shutdown": {
                "until": self.pending_until,
                "left_sec": max(0, self.pending_until - now_ts()) if self.pending_until else 0,
                "reason": self.pending_reason,
                "will_exec": self._will_exec(),
            } if self.pending_until else None,
        }

    def _driver_options(self):
        """列出所有驱动候选 + 可用性 + 是否当前生效（30s 缓存，避免反复 spawn subprocess）。"""
        if self._driver_opts_cache and time.time() - self._driver_opts_t < 30:
            return self._driver_opts_cache
        cur_kind = getattr(self.driver, "kind", "?")
        cur_mode = (self.cfg.get("driver") or "auto").lower()
        opts = []
        for kind, label, klass in [
            ("auto", "自动（NUT+HID 探测，按 preferred 优先级）", None),
            ("nut", "NUT 桥接（支持数百款常见 UPS）", NutBridgeDriver),
            ("hid", "自研 HID（WalleCube 等小众 UPS）", HidUpsDriver),
            ("mock", "模拟（调试用，无硬件兜底）", MockDriver),
        ]:
            avail = None
            if klass is not None:
                try:
                    d = klass(self.cfg)
                    avail = bool(d.available())
                except Exception as e:
                    log("driver_options: probe %s failed: %s" % (kind, e))
                    avail = False
            active = (kind == cur_kind)     # 仅实际生效驱动 active；auto 不是驱动故不会 active（模式看 driver_mode 字段）
            opts.append({
                "kind": kind,
                "label": label,
                "available": avail,
                "active": active,
            })
        self._driver_opts_cache = opts
        self._driver_opts_t = time.time()
        return opts


# ============================================================ Web
class Web:
    def __init__(self, cfg, store, engine, cfg_path):
        self.cfg = cfg
        self.store = store
        self.engine = engine
        self.cfg_path = cfg_path

    # ---- 鉴权 ----
    def token(self):
        return hashlib.sha256(("upsmgr:" + str(self.cfg.get("password"))).encode()).hexdigest()

    def authed(self, headers):
        c = headers.get("Cookie", "")
        for part in c.split(";"):
            if part.strip().startswith("ups_token="):
                return part.strip().split("=", 1)[1] == self.token()
        return False


def render_index():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html"),
              "r", encoding="utf-8") as f:
        return f.read()


class Handler(BaseHTTPRequestHandler):
    server_version = "upsmgr/" + APP_VERSION

    def log_message(self, fmt, *args):
        pass

    # ---- 响应工具 ----
    def _json(self, obj, code=200, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie + "; Path=/; HttpOnly; SameSite=Lax")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _page(self, text):
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---- 路由 ----
    def do_GET(self):
        w = self.server.web
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            self._page(render_index())
            return
        if path == "/api/login":
            self._json({"ok": True, "token": w.token()})
            return
        if not w.authed(self.headers):
            self._json({"ok": False, "error": "unauthorized"}, 401)
            return
        if path == "/api/status":
            snap = w.engine.snapshot()
            snap["version"] = APP_VERSION
            snap["cfg"] = {k: v for k, v in w.cfg.items() if k != "password"}
            snap["mock_available"] = isinstance(w.engine.driver, MockDriver)
            self._json(snap)
        elif path == "/api/history":
            mins = 360
            try:
                q = self.path.split("?", 1)[1]
                for kv in q.split("&"):
                    if kv.startswith("minutes="):
                        mins = max(5, min(1440, int(kv.split("=")[1])))
            except Exception:
                pass
            self._json({"rows": w.store.history(mins)})
        elif path == "/api/events":
            self._json({"rows": w.store.events(120)})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        w = self.server.web
        path = self.path.split("?", 1)[0]
        body = self._body()
        if path == "/api/login":
            if str(body.get("password", "")) == str(w.cfg.get("password")):
                tok = w.token()
                self._json({"ok": True}, cookie="ups_token=" + tok)
            else:
                self._json({"ok": False, "error": "口令不对"}, 403)
            return
        if not w.authed(self.headers):
            self._json({"ok": False, "error": "unauthorized"}, 401)
            return
        if path == "/api/settings":
            allowed = {"driver", "driver_preferred", "shutdown_mode",
                       "poll_sec", "low_batt_pct", "shutdown_batt_pct",
                       "shutdown_delay_min", "enable_shutdown", "dry_run",
                       "shutdown_command", "pre_shutdown_commands",
                       "password", "history_days", "notify_enabled", "notify_uid"}
            changed = []
            for k, v in body.items():
                if k not in allowed:
                    continue
                if k in ("enable_shutdown", "dry_run", "notify_enabled"):
                    v = bool(v)
                elif k in ("poll_sec", "shutdown_delay_min", "history_days", "notify_uid"):
                    v = max(1, int(v))
                elif k in ("low_batt_pct", "shutdown_batt_pct"):
                    v = min(100, max(0, int(v)))
                elif k == "password":
                    v = str(v).strip()
                    if not v:
                        continue
                elif k in ("driver", "driver_preferred", "shutdown_mode"):
                    v = str(v).strip().lower()
                    if k == "driver" and v not in ("auto", "hid", "nut", "mock"):
                        continue
                    if k == "driver_preferred" and v not in ("hid", "nut"):
                        continue
                    if k == "shutdown_mode" and v not in ("battery", "timer"):
                        continue
                changed.append(k)
                w.cfg[k] = v
            if changed:
                with open(w.cfg_path, "w", encoding="utf-8") as f:
                    json.dump(w.cfg, f, ensure_ascii=False, indent=2)
                w.engine.reload_config(w.cfg)
                if "driver" in changed or "driver_preferred" in changed:
                    w.engine.rebuild_driver()
                self._json({"ok": True, "changed": changed})
            else:
                self._json({"ok": False, "error": "没有可保存的字段"})
        elif path == "/api/mock":
            r = w.engine.mock(body.get("action"), body.get("value"))
            if r is None:
                self._json({"ok": False, "error": "当前不是模拟驱动"})
            else:
                self._json({"ok": True, "reading": r})
        elif path == "/api/cancel_shutdown":
            if w.engine.pending_until:
                w.engine.pending_until = None
                w.store.add_event("info", "shutdown_cancelled", "用户在面板取消了预关机")
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": "not found"}, 404)


# ============================================================ main
def main():
    cfg = dict(DEFAULT_CONFIG)
    cfg_path = os.environ.get("UPSMGR_CONFIG", "/data/upsmgr.json")
    data_dir = os.environ.get("UPSMGR_DATA", os.path.dirname(cfg_path) or ".")
    for base in (cfg_path,):
        try:
            with open(base, "r", encoding="utf-8") as f:
                user = json.load(f)
                for k, v in user.items():
                    cfg[k] = v
        except FileNotFoundError:
            os.makedirs(data_dir, exist_ok=True)
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log("config load failed (%s): %s" % (cfg_path, e))
    cfg["data_dir"] = data_dir

    db_path = os.path.join(data_dir, "upsmgr.db")
    store = Store(db_path)
    engine = Engine(cfg, store)
    web = Web(cfg, store, engine, cfg_path)

    port = int(cfg.get("port", 9750))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.web = web
    httpd.daemon_threads = True
    store.add_event("info", "startup", "%s %s 已启动" % (APP_NAME, APP_VERSION))
    log("%s %s listening on :%d (driver=%s)" % (
        APP_NAME, APP_VERSION, port, getattr(engine.driver, "kind", "?")))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
