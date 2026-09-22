# -*- coding: utf-8 -*-
"""ZeroNAS 桌面注册「电源管理」图标 + 快捷方式

用法:
    python register_icon.py http://NAS_IP:9750
    python register_icon.py http://NAS_IP:9750 https://your-domain.example.com
    python register_icon.py http://NAS_IP:9750 --ssh-port 10022 --ssh-key ~/.ssh/id_rsa
    python register_icon.py http://NAS_IP:9750 --dry-run        # 只看将要做的改动，不落库

    第二个参数（可选）是公网访问基地址（如铁牛link 的域名）：
      - 传了 → 图标登记为 绝对地址 base + /pc/upsmon-icon.png，
        手机 APP（原生图片加载器，不认相对路径）也能正常显示；
      - 没传 → 图标登记为相对路径 /pc/upsmon-icon.png，
        网页端本地 / 远程都能显示，但手机 APP 图标会空白。

要求:
    1. NAS 已开启 SSH，且当前电脑能免密（或交互式输入密码）登录
    2. upsmgr 容器已在运行（面板地址能打开）

功能:
    - 在应用中心注册 upsmon 应用（icon_url / 描述 / 端口 9750）
    - 为桌面用户添加快捷方式（自动复用已有 user_id，默认 1000）
    - 图标部署到 NAS 的对外网页目录 /usr/local/pc/upsmon-icon.png，
      应用中心登记为 /pc/upsmon-icon.png —— 局域网和反向代理远程访问都能加载，
      不再依赖内网 IP 直连（内网 IP 远程加载不到，图标会裂图）
    - 每次执行前自动备份 appstore.db -> appstore.db.bak-upsmon
    - 可重复执行；已存在同 code 的行时只更新展示字段，
      不会覆盖 download_url / install_location（不影响 .tpk 那条安装链）

注意:
    本脚本只适用于「Docker 手动部署」的场景。如果你的 upsmgr 是走应用中心
    以 .tpk 安装的，不需要执行本脚本（那条路已经自带图标与快捷方式）。
    系统大版本更新可能清空 /usr/local/pc 下的自定义文件，图标消失时重跑一次即可。
"""
import base64
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

APP_CODE = "upsmon"
APP_ID = "com.centerm.docker.upsmon"
APP_NAME = "电源管理"
APP_NAME_EN = "Power Manager"
APP_VER = "0.1.3"
APP_PORT = "9750"
WEBROOT = "/usr/local/pc"
DB = "/userdata/db/appstore.db"

DESC_ZH = ("铁牛NAS UPS 电源管理：实时监控市电/电池状态、电量、输出功率与续航，"
           "断电且电量不足或中断超时后按规则自动安全关机以保护数据，"
           "关键事件推送到铁牛通知栏，Web 面板手机电脑可用。")
DESC_EN = ("UPS power manager for your NAS: live mains/battery status, charge, output power and "
           "runtime, with rule-based safe shutdown when power is out and the battery runs low, "
           "notifications to the NAS message center, and a mobile-friendly web panel.")
FEAT_ZH = ("v" + APP_VER + " 更新内容：\n"
           "· 电量读不到时不再按 0% 误触发关机\n"
           "· UPS 报告「即将断电」时纳入关机流程\n"
           "· 输出功率改用独立瓦特轴，曲线不再贴在图底")
FEAT_EN = ("v" + APP_VER + " changes:\n"
           "- No more spurious shutdown when battery charge is unreadable\n"
           "- Handle UPS FSD (shutdown imminent) in the shutdown flow\n"
           "- Output power now drawn on its own watt axis")


def usage():
    print(__doc__)
    return 1


def parse_args(argv):
    pos, opt = [], {"ssh_user": "root", "ssh_port": None, "ssh_key": None, "dry": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--dry-run":
            opt["dry"] = True
        elif a == "--ssh-user" and i + 1 < len(argv):
            i += 1
            opt["ssh_user"] = argv[i]
        elif a == "--ssh-port" and i + 1 < len(argv):
            i += 1
            opt["ssh_port"] = argv[i]
        elif a == "--ssh-key" and i + 1 < len(argv):
            i += 1
            opt["ssh_key"] = os.path.expanduser(argv[i])
        elif a.startswith("-"):
            print("未知参数: %s" % a)
            return None, None
        else:
            pos.append(a)
        i += 1
    return pos, opt


def fetch_icon_bytes(host):
    """优先取仓库自带的 icon.png，取不到再从面板 favicon 兜底"""
    here = os.path.dirname(os.path.abspath(__file__))
    local = os.path.join(here, os.pardir, "icon.png")
    if os.path.isfile(local):
        with open(local, "rb") as f:
            data = f.read()
        return data, "仓库 icon.png"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        data = opener.open(host + "/favicon.ico", timeout=15).read()
        return data, "面板 /favicon.ico"
    except Exception as e:
        raise SystemExit("取不到图标（仓库 icon.png 不存在，面板也拉不到：%s）" % e)


REMOTE_TMPL = r'''
import base64, json, os, shutil, sqlite3, time

DB = __DB__
WEBROOT = __WEBROOT__
CODE = __CODE__
ICON_URL = __ICONURL__
DRY = __DRY__

backup = DB + ".bak-upsmon"
if not DRY:
    shutil.copyfile(DB, backup)
    print("备份数据库 ->", backup)
else:
    print("[dry-run] 将备份数据库 ->", backup)

# 1) 图标落地到对外网页目录
dst = os.path.join(WEBROOT, CODE + "-icon.png")
if not DRY:
    os.makedirs(WEBROOT, exist_ok=True)
    with open(dst, "wb") as f:
        f.write(base64.b64decode(__ICONB64__))
    os.chmod(dst, 0o644)
print("图标 -> %s (%d 字节)" % (dst, __ICONSIZE__))

cfg = __CFG__

now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + ".000000000+08:00"
today = time.strftime("%Y-%m-%d")

c = sqlite3.connect(DB, timeout=15)
c.row_factory = sqlite3.Row
cols = [r[1] for r in c.execute("PRAGMA table_info(appstore_app)")]
old = c.execute("SELECT * FROM appstore_app WHERE code=?", (CODE,)).fetchone()

if old is None:
    row = {k: None for k in cols}
    row.update({
        "code": CODE, "icon_url": ICON_URL, "state": "started", "shelf_state": 1,
        "service_name": CODE, "type": 1, "config": cfg,
        "latest_config": cfg, "latest_icon_url": ICON_URL,
        "install_location": "", "download_url": "", "download_progress": 0,
        "package_size": __PKGSIZE__, "latest_package_size": __PKGSIZE__,
        "carousel_img_urls": "", "latest_carousel_img_urls": "",
        "version": __VER__, "latest_version": __VER__,
        "release_date": today, "latest_release_date": today,
        "latest_version_content": __FEAT__,
        "sort": 99, "need_update": 0, "is_shortcut": 1,
        "create_time": now, "update_time": now,
        "licence_agreement_link": "", "source_code_link": "",
        "documentation": "",
    })
    row = {k: v for k, v in row.items() if k in cols}
    sql = "INSERT INTO appstore_app (%s) VALUES (%s)" % (
        ",".join(row.keys()), ",".join("?" * len(row)))
    print("新增应用行:", CODE)
else:
    # 已存在（例如先用 .tpk 装过）→ 只更新展示字段，不动 download_url/install_location，
    # 否则应用中心里那条安装链会被这次注册覆盖掉。
    row = {"icon_url": ICON_URL, "latest_icon_url": ICON_URL,
           "service_name": CODE, "state": "started", "shelf_state": 1,
           "config": cfg, "latest_config": cfg,
           "version": __VER__, "latest_version": __VER__,
           "latest_release_date": today, "latest_version_content": __FEAT__,
           "sort": 99, "need_update": 0, "is_shortcut": 1, "update_time": now}
    row = {k: v for k, v in row.items() if k in cols}
    sql = "UPDATE appstore_app SET %s WHERE code=?" % ",".join(
        "%s=?" % k for k in row.keys())
    print("更新已有应用行:", CODE,
          "(保留 download_url=%r)" % (old["download_url"] or ""))

if DRY:
    print("[dry-run] SQL:", sql)
    print("[dry-run] 字段:", ", ".join(sorted(row.keys())))
else:
    c.execute(sql, list(row.values()) + ([CODE] if old is not None else []))
    print("应用中心写入完成")

# 2) 桌面快捷方式
ur = c.execute("SELECT user_id FROM appstore_app_shortcut LIMIT 1").fetchone()
uid = str(ur["user_id"]) if ur else "1000"
if DRY:
    print("[dry-run] 快捷方式: user_id=%s app_code=%s" % (uid, CODE))
else:
    c.execute("INSERT OR REPLACE INTO appstore_app_shortcut "
              "(user_id, app_code, operate_type, create_time, update_time) "
              "VALUES (?, ?, 1, ?, ?)", (uid, CODE, now, now))
    c.commit()
    r = c.execute("SELECT code,state,icon_url,shelf_state FROM appstore_app "
                  "WHERE code=?", (CODE,)).fetchone()
    print("应用行:", dict(r) if r else None)
    s = c.execute("SELECT user_id,app_code FROM appstore_app_shortcut "
                  "WHERE app_code=?", (CODE,)).fetchone()
    print("快捷方式:", dict(s) if s else None)
c.close()
print("DONE")
'''


def main():
    pos, opt = parse_args(sys.argv[1:])
    if pos is None:
        return 1
    if not pos:
        return usage()

    host = pos[0].rstrip("/")
    public = pos[1].rstrip("/") if len(pos) > 1 else ""
    if public.endswith("/pc"):
        public = public[:-3]

    ip = urllib.parse.urlparse(host).hostname or host.split(":")[0]
    icon_url = (public + "/pc/%s-icon.png" % APP_CODE) if public else "/pc/%s-icon.png" % APP_CODE

    icon_bytes, icon_src = fetch_icon_bytes(host)

    cfg = {
        "appId": APP_ID,
        "serviceName": APP_CODE,
        "version": {"lowVersion": "0.1.0", "version": APP_VER},
        "languageList": ["zh-CN", "en-US"],
        "i18n": [
            {"name": APP_NAME, "description": DESC_ZH, "author": "西了个瓜",
             "langName": "zh-CN", "versionContent": FEAT_ZH},
            {"name": APP_NAME_EN, "description": DESC_EN, "author": "西了个瓜",
             "langName": "en-US", "versionContent": FEAT_EN},
        ],
        "accessCtrl": {
            "urlAppAccesses": [
                {"httpsEnable": False, "httpsPort": "", "port": APP_PORT, "urlPath": "/"}
            ],
            "supports": ["pc", "app"],
        },
    }

    remote = (REMOTE_TMPL
              .replace("__DB__", json.dumps(DB))
              .replace("__WEBROOT__", json.dumps(WEBROOT))
              .replace("__CODE__", json.dumps(APP_CODE))
              .replace("__ICONURL__", json.dumps(icon_url))
              .replace("__DRY__", "True" if opt["dry"] else "False")
              .replace("__ICONB64__", json.dumps(base64.b64encode(icon_bytes).decode()))
              .replace("__ICONSIZE__", str(len(icon_bytes)))
              .replace("__CFG__", json.dumps(json.dumps(cfg, ensure_ascii=False), ensure_ascii=False))
              .replace("__VER__", json.dumps(APP_VER))
              .replace("__FEAT__", json.dumps(FEAT_ZH, ensure_ascii=False))
              .replace("__PKGSIZE__", "60"))

    print("目标 NAS : %s（SSH %s@%s%s）"
          % (ip, opt["ssh_user"], ip,
             ":" + opt["ssh_port"] if opt["ssh_port"] else ""))
    print("面板地址 : %s" % host)
    print("图标来源 : %s（%d 字节）" % (icon_src, len(icon_bytes)))
    print("图标登记 : %s%s"
          % (icon_url, "" if public else "   ← 未传公网基地址，手机 APP 图标可能空白"))
    print("模式     : %s" % ("dry-run（不落库）" if opt["dry"] else "实际执行"))
    print("-" * 60)

    ssh = ["ssh"]
    if opt["ssh_port"]:
        ssh += ["-p", str(opt["ssh_port"])]
    if opt["ssh_key"]:
        ssh += ["-i", opt["ssh_key"]]
    ssh += ["%s@%s" % (opt["ssh_user"], ip), "python3 -"]

    try:
        p = subprocess.run(ssh, input=remote, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        print("找不到 ssh 命令：请安装 OpenSSH 客户端，或在 NAS 上手动执行本脚本")
        return 1
    except subprocess.TimeoutExpired:
        print("SSH 执行超时（120 秒）")
        return 1

    print(p.stdout or "")
    if p.returncode != 0:
        print("SSH 失败 (rc=%s)：\n%s" % (p.returncode, (p.stderr or "").strip()[-800:]))
        print("请确认：① NAS 已开启 SSH；② 端口/用户名正确（--ssh-port / --ssh-user）；"
              "③ 已配置免密登录或能输入密码")
        return 1

    if opt["dry"]:
        print("dry-run 结束，未改动任何数据。去掉 --dry-run 即实际执行。")
    else:
        print("完成！刷新 ZeroNAS 桌面即可看到「%s」图标" % APP_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
