# upsmgr — NAS UPS 电源管理（铁牛OS / 铁牛NAS / ZeroNAS）

> 单容器、零依赖的 NAS UPS 电源管理守护进程 + Web 控制台：与硬件 UPS 通讯，断电时按规则自动安全关机。

纯 Python 标准库实现（无需 pip 安装任何包），通过 NUT 桥接驱动支持数百款常见 UPS
（APC / CyberPower / MSK / WalleCube 等），并提供一个可视化 Web 界面（默认 9750 端口），
实时监控市电状态、电池电量、负载与续航，在电力中断且电量不足时按规则触发自动安全关机以保护数据。
已在 Centerm Zero 1 Pro（铁牛OS / ZeroNAS）+ WalleCube Smart UPS W150 上长期稳定运行，
理论上适用于任何带 Docker 的 x86 NAS。

## 功能特性

- **NUT 桥接驱动**：基于 [Network UPS Tools](https://networkupstools.org/) 的 `usbhid-ups` 通用 HID 驱动，开箱支持 APC / CyberPower / MSK / WalleCube 等数百款 UPS。
- **状态实时监控**：市电 / 电池供电切换、电池电量、输出功率、电池电压、温度、预估续航一目了然。
- **关机规则可配**：电量不足阈值触发，或市电中断持续时长触发，二选一；60 秒预关机窗口可在面板一键取消。
- **UPS 主动通知（FSD）**：UPS 自报「即将断电」时直接进入关机流程，不再只靠本机推算。
- **铁牛通知栏推送**：断电 / 低电 / 恢复市电三类关键事件自动推送到铁牛 OS 通知栏（`common.msgcenter.services.Notice` 服务）。系统模板自带的「测试消息」前缀会在安装 / 更新时被自动等长补丁去掉（备份 `/volume1/data/filemanage.bak.latest`；系统升级还原二进制后重跑 `tools/patch_msg_prefix.sh` 或重装本应用即可）。
- **历史曲线 + 事件日志**：近 6 小时电量 / 输出功率曲线，所有事件落库可查。
- **安全双保险**：默认仅监控提醒，启用「UPS 支持」开关后才执行关机；关机命令可 chroot 落到宿主系统。
- **体验细节**：深浅色主题一键切换（localStorage 记忆）、手机端自适应、铁牛OS / ZeroNAS 桌面图标一键注册。

## 适用机型与系统

在 **Centerm Zero 1 Pro + WalleCube Smart UPS W150** 上开发并长期实机运行，已验证 / 可用的平台：

| 平台 / 系统 | 支持情况 |
|---|---|
| **铁牛OS · 铁牛NAS · ZeroNAS**（Centerm Zero 系列） | ⭐ 原生支持：Docker Compose 一键部署、应用中心 `.tpk` 安装、桌面图标注册 |
| 任何带 USB 接口的 x86 NAS + 兼容 UPS | ✅ 有 Docker + NUT `usbhid-ups` 能识别的 UPS 即可 |
| 群晖 DSM / 威联通 QTS / TrueNAS / UNRAID / PVE / 自建 x86 NAS | ✅ 同上，只需 root 与 Docker，USB 接 UPS |

> 搜索关键词：铁牛 NAS、铁牛NAS、铁牛OS、tieniu nas、ZeroNAS UPS、NAS 不间断电源、NAS 自动关机、Network UPS Tools、NUT usbhid-ups、WalleCube Smart UPS。

## 环境要求

- x86 NAS，已安装 Docker / Docker Compose
- 一台被 NUT `usbhid-ups` 驱动支持的 USB UPS（不支持的小众型号可走内置自研 HID 兜底）
- 需要 **root / privileged** 权限（读 USB HID 设备 + 执行关机命令）
- 构建镜像时需要能访问 Debian 软件源（NUT 在构建期装入镜像，国内可换源，见下）

## 包内容

| 文件 | 说明 |
|---|---|
| `app/upsmgr.py` | 主程序（单文件，容器内运行） |
| `app/panel.html` | 内嵌 Web 控制台 |
| `app/run.sh` | 容器入口：NUT 自包含集成（写配置、起 `usbhid-ups` + `upsd`）后启动主程序 |
| `Dockerfile` | 镜像定义（`python:3.12-slim` + 构建期装好 NUT） |
| `docker-compose.yml` | Compose 编排（特权 + host 网络 + `/dev` + 数据卷） |
| `tools/verify.py` | 安装 / 重启后一键验证（面板、驱动、电量、关机规则、事件） |
| `tools/register_icon.py` | 注册铁牛OS / ZeroNAS 桌面「电源管理」图标 + 快捷方式 |
| `tools/patch_msg_prefix.sh` | 去掉铁牛通知「测试消息」前缀（手动执行版，tpk 安装会自动跑） |
| `tools/hidparse.py` | 通用 HID 报告描述符解析器，用于排查 NUT 不识别的 UPS |
| `tools/test_driver_options.py` | 驱动选择器接口回归测试（本地自起服务，只用标准库） |
| `icon.png` | 应用图标（`register_icon.py` 部署到 NAS 对外网页目录用） |
| `CHANGELOG.md` | 版本更新记录 |
| `LICENSE` | MIT 开源许可 |

## 安装步骤（铁牛OS / ZeroNAS，走应用中心）

### 1. 接好 UPS

把 USB UPS 用 USB 线接到 NAS 的 USB 口，UPS 通电。在 NAS 上 `ls /dev/hidraw*` 应能看到设备节点。

### 2. 安装 .tpk

通过铁牛应用中心上传 `upsmon_<version>.tpk` 安装包，或用 [nas-appinstall](https://github.com/hahaha-9527/nas-appinstall)
工具一键注册到应用中心。安装完成后桌面会出现「电源管理」图标。

### 3. 打开控制台

浏览器打开 `http://NAS_IP:9750`，默认口令 `admin`（首装后请在「高级设置」里立即修改）。

### 4. 配置关机规则

1. 点「编辑 UPS 规则」选关机等待方式：
   - **直到电量不足**：UPS 电池电量低于阈值（默认 30%）时自动关机
   - **自定义时间**：市电中断持续 N 分钟后自动关机（默认 5 分钟）
2. 设置低电提醒阈值（电量低于该值时推送通知，不影响关机）。
3. 开启「启用 UPS 支持」开关，断电规则即生效。

### 5. 验证

```
python tools/verify.py http://NAS_IP:9750 口令
```

控制台首页应显示当前 UPS 的品牌型号、电量、续航、供电类型；事件日志里应有一条「电源管理 vX.X.X 已启动」。

## 安装步骤（任意 Linux Docker 主机）

不需要应用中心，有 Docker 就能跑：

```bash
tar -xzf nas-upsmgr-v0.1.3.tar.gz     # Windows 用户用 .zip
cd nas-upsmgr-v0.1.3
docker compose up -d --build
```

打开 `http://主机IP:9750`，默认口令 `admin`。

国内构建慢时换 APT 源：

```bash
docker build --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn -t upsmon:0.1.3 .
docker compose up -d            # 镜像已存在则不再构建
```

**USB 设备**：镜像以 `privileged: true` + 全量 `/dev` 运行，`/dev/hidraw*` 可直接访问。
想收紧权限可只映射具体节点：

```yaml
    devices:
      - /dev/hidraw0:/dev/hidraw0
      - /dev/bus/usb:/dev/bus/usb
    # privileged: true   # 去掉
```

> 个别发行版 hidraw 节点默认 600 且属主 root，容器内 root 可直读；若仍报权限不足，
> 在宿主机加一条 udev 规则或临时 `chmod 0666 /dev/hidraw0`。

**关机命令要注意**：容器内执行 `shutdown` 只会停掉容器本身。要让宿主机真关机，把宿主根挂进来即可
（挂载后 `shutdown_command` 会自动 `chroot` 到宿主执行）：

```yaml
    volumes:
      - /:/host
```

或者改用主机侧通道，例如 `shutdown_command: "curl -fsS http://127.0.0.1:主机管理端口/关机接口"`。

## 配置

所有配置落在 `./data/upsmgr.json`（首次启动自动生成默认值）：

| 字段 | 默认 | 说明 |
|---|---|---|
| port | 9750 | 面板端口（host 网络下即宿主端口） |
| password | admin | 面板口令 |
| driver_preferred | nut | NUT 桥接优先（兼容数百款 UPS）；自研 HID 仅兜底 |
| low_batt_pct | 50 | 低电量告警线 |
| shutdown_batt_pct | 30 | 触发预关机的电量线 |
| shutdown_delay_min | 5 | 或断电时长触发线（二者先到先触发） |
| enable_shutdown | false | **默认 false = 只监控不关机**；开启后面板可取消预关机 |
| shutdown_command | /sbin/shutdown -h now | 宿主机关机命令（容器内需能作用到宿主，见上） |

改完配置 `docker compose restart` 生效；改 `app/` 下的程序文件则 `docker compose up -d --force-recreate`。

## 常见问题

**应用中心图标整排破图** → 应用中心界面是客户端本地页面（`file://` 加载），图标 `icon_url` 用相对路径会全挂。
本应用采用 base64 内嵌方案（`data:image/png;base64,...`），与协议 / 网络 / origin 全无关，不应出现此问题；
若仍破图，检查 `appstore_app.icon_url` 字段是否被外部 sync 覆盖。

**容器日志显示 `insufficient permissions on everything`** → NUT 的 `usbhid-ups` 默认以 `nut` 用户跑，
读不到 root 600 的 `/dev/hidraw*`。`app/run.sh` 已用 `user = root` + `upsd -u root` + `chmod 0666 /dev/hidraw*` 解决；
若仍报错，确认容器是 `privileged: true` 且 `devices: /dev:/dev` 映射到位。

**`ups.status: ALARM` 或 `ups.alarm: No battery installed!`** → 部分 UPS 偶发自报故障，重启 UPS 或重新插拔 USB 即可恢复。

**`battery.runtime: 65534`** → NUT 占位值，表示市电供电时续航无限大。面板已识别为「市电供电中」，不会误显成 1092 分钟。

**负载曲线一直是空的 / 显示的是「输出功率 W」** → 有些 UPS（如 WalleCube W150 走 openUPS HID）不上报 `ups.load`
负载百分比。此时面板改画「输出电压 × 输出电流」推算出的输出功率（独立瓦特轴），不是故障。

**NUT 不识别我的 UPS** → 自研 HID 驱动可作兜底，但当前默认关闭。需要支持小众 UPS 的，
把 `driver_preferred` 改为 `hid` 走自研 HID（需针对该型号单独写解码，`tools/hidparse.py` 可辅助分析报告描述符）。

**关机命令没执行** → 默认 `enable_shutdown=false`，仅在面板开启「启用 UPS 支持」开关后才会真关机。
预关机 banner 上会标注「将执行关机」/「开关未启用，不会真关」。

## 相关项目

同系列工具，都在 Centerm Zero 1 Pro（铁牛OS）上实机跑通 —— 纯 Python 标准库、单容器、MIT：

| 项目 | 用途 |
| --- | --- |
| [nas-appinstall](https://github.com/hahaha-9527/nas-appinstall) | 网页版 `.tpk` 上传口子：把本地应用包注册进应用中心并完成安装 / 升级 |
| [nas-tieniuled](https://github.com/hahaha-9527/nas-tieniuled) | 机箱电源灯 / 硬盘灯的可视化控制台 |
| [nas-fanctl](https://github.com/hahaha-9527/nas-fanctl) | 风扇温度调速：按 CPU 与硬盘温度自动调节转速 |
| [nas-cloudmount](https://github.com/hahaha-9527/nas-cloudmount) | 网盘挂载：用 rclone 把 AList 的 WebDAV 桥接成 NAS 上的真实目录 |

## 版本记录

各版本的新增与修复详见 [CHANGELOG.md](CHANGELOG.md)。

## 许可

[MIT](LICENSE) © 2026 西了个瓜

> 本项目在真实硬件上反复调试而成，关机规则默认值是针对 WalleCube Smart UPS W150（LiFePO4 电池、市电供电时续航无限大）的实测调优结果，其他 UPS 请按实际电池电压自行微调。
