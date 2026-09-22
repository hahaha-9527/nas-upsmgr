# -*- coding: utf-8 -*-
"""upsmgr 安装 / 重启后验证

用法:
    python verify.py http://NAS_IP:9750 你的面板口令

检查项:
    1. 面板是否在线（GET /api/status）
    2. 驱动是否就绪（当前驱动、连接状态、候选驱动可用性）
    3. 实时读数：供电类型、电量、输出功率 / 负载、续航、电池电压、温度
    4. 关机规则：模式、阈值、预关机窗口、是否会真的执行关机
    5. 最近事件（市电中断 / 恢复 / 低电 / 启动）

退出码：0 全部正常；1 有致命问题（面板不可达 / 未连上 UPS）。
"""
import json
import sys
import time
import urllib.request

if len(sys.argv) < 3:
    print(__doc__)
    sys.exit(1)

HOST = sys.argv[1].rstrip("/")
PWD = sys.argv[2]
# 绕过系统代理：局域网地址走代理常常直接失败
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path, cookie=None):
    req = urllib.request.Request(HOST + path)
    if cookie:
        req.add_header("Cookie", cookie)
    r = opener.open(req, timeout=15)
    return json.loads(r.read().decode("utf-8"))


def main():
    print("目标: %s" % HOST)

    # ---- 1) 在线 + 登录 ----
    try:
        login = api("/api/login")
    except Exception as e:
        print("面板不可达 ❌  %s" % e)
        print("  · 确认容器在跑：docker ps | grep upsmon")
        print("  · 确认端口是否正确（默认 9750）")
        return 1
    cookie = "ups_token=" + login["token"]

    try:
        s = api("/api/status", cookie)
    except Exception as e:
        print("面板可达，但状态接口异常 ❌  %s" % e)
        return 1
    if s.get("error"):
        print("状态接口返回错误 ❌  %s" % s["error"])
        return 1
    print("面板在线 ✅    版本 %s" % s.get("version"))

    # ---- 2) 驱动 ----
    print("\n=== 驱动 ===")
    print("  当前驱动 = %s（%s）   模式 = %s   偏好 = %s"
          % (s.get("driver_kind"), s.get("driver_name"),
             s.get("driver_mode"), s.get("driver_preferred")))
    print("  连接状态 = %s   供电状态 = %s"
          % ("已连上 UPS ✅" if s.get("connected") else "未连上 ❌", s.get("status")))
    for o in s.get("driver_options") or []:
        print("    %-5s %-38s 可用=%s%s"
              % (o.get("kind"), o.get("label"), o.get("available"),
                 "   ← 当前生效" if o.get("active") else ""))

    # ---- 3) 读数 ----
    r = s.get("reading") or {}
    print("\n=== 实时读数（%s）===" % (r.get("model") or "-"))
    print("  电量 = %s%%    输出功率 = %s W    负载 = %s%%"
          % (r.get("charge_pct"), r.get("output_w"), r.get("load_pct")))
    rt = r.get("runtime_min")
    print("  续航 = %s    输入电压 = %s V    输出电压 = %s V"
          % ("市电供电中（无限）" if rt is None or rt >= 999 else "%s 分钟" % rt,
             r.get("input_v"), r.get("output_v")))
    print("  电池电压 = %s V    温度 = %s °C" % (r.get("battery_v"), r.get("temp_c")))
    if r.get("load_pct") is None and r.get("output_w") is not None:
        print("  · 该 UPS 不上报负载百分比，面板改画「输出功率 W」（独立瓦特轴），属正常")

    # ---- 4) 关机规则 ----
    cfg = s.get("cfg") or {}
    ps = s.get("pending_shutdown")
    print("\n=== 关机规则 ===")
    print("  模式 = %s   低电告警线 = %s%%   关机电量线 = %s%%   断电时长线 = %s 分钟"
          % (cfg.get("shutdown_mode") or "battery", cfg.get("low_batt_pct"),
             cfg.get("shutdown_batt_pct"), cfg.get("shutdown_delay_min")))
    print("  启用 UPS 支持 = %s   试运行（只记日志不真关） = %s"
          % (cfg.get("enable_shutdown"), cfg.get("dry_run")))
    print("  关机命令 = %s" % cfg.get("shutdown_command"))
    if not cfg.get("enable_shutdown"):
        print("  · 当前只监控不关机（出厂默认）；确认规则无误后再去面板打开开关")
    if ps:
        print("  ⚠️ 预关机倒计时中：%s 秒后执行（原因 %s，会真关=%s）"
              % (ps.get("left_sec"), ps.get("reason"), ps.get("will_exec")))
    elif s.get("on_battery_since"):
        mins = (time.time() - float(s["on_battery_since"])) / 60.0
        print("  当前电池供电中，已持续 %.1f 分钟" % mins)

    # ---- 5) 事件 ----
    try:
        ev = api("/api/events", cookie)
        rows = (ev.get("rows") or [])[:8]
        print("\n=== 最近事件 ===")
        if rows:
            for e in rows:
                ts = time.strftime("%m-%d %H:%M", time.localtime(float(e.get("ts") or 0)))
                print("  %s  %-5s %-15s %s" % (ts, e.get("level"), e.get("type"), e.get("message")))
        else:
            print("  （暂无事件）")
    except Exception as e:
        print("\n事件接口读取失败（不影响主流程）: %s" % e)

    print("\n验证完成")
    return 0 if s.get("connected") else 1


if __name__ == "__main__":
    sys.exit(main())
