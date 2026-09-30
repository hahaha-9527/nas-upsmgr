# Changelog

电源管理 upsmgr 的版本更新记录。版本号格式：`v<主>.<次>.<修订>`。

## v0.1.4 — 2026-10-01

### 修复

- **UPS 拔插 / USB 掉线重连后能自动恢复**。此前必现「UPS 通讯丢失」，只能靠重启应用救回来：容器用的是自己的 devtmpfs，宿主重新枚举出来的 `/dev/bus/usb/001/00N` 在容器里根本不存在，libusb 直接 `No such device` → `upsc` 报 `Data stale`，而容器里没有 supervisor，永远不会自愈。现在按 `/sys` 的 MAJOR/MINOR 补齐设备节点并自动重拉 NUT 栈（`usbhid-ups` + `upsd`），实测 1 秒内恢复读数。
- **短时抖动不再误报「通讯丢失」**：读数失败先进入宽限期（默认 120 秒）并在期间反复自愈，超过宽限期才报警。
- **重连后的读数有一段不可信期**（默认 20 秒）：刚上电时 UPS 常乱报（如 `alarm=No battery installed`），期间只展示、不触发告警与关机判据。
- **auto 模式不再选中"识别得到但读不出数"的驱动**：未校准的自研 HID 驱动只有 `status=detected`、字段全为 None，现在不算可用；当前驱动读不到数时会自动重选可用驱动。
- `GET /api/login` 不再无条件下发 token（需已登录会话或凭口令）。
- **清理失效的设备节点（幽灵节点）**。设备重插时按 sysfs 补齐出来的 `/dev/hidraw*`、
  `/dev/usb/hiddev*` 在设备被 libusb 接管后就没了对应的 sysfs 来源，但节点会永久留在
  容器里、打开必然报 ENODEV，还让 HID 驱动每轮探测失败、日志持续刷屏。现在节点跟随
  sysfs 回收：连续 3 轮在 sysfs 里查不到才删除（留 3 轮是为了不被重新枚举时的闪断误伤）。
- **修正文档里一处危险误导**：`dry_run` 是历史遗留字段、策略早已不引用；此前注释写着
  「dry_run=True 时只记录不执行」，实际上唯一的总开关只有 `enable_shutdown`。
- **清理失效的设备节点（幽灵节点）**。设备重插时按 sysfs 补齐出来的 `/dev/hidraw*`、
  `/dev/usb/hiddev*` 在设备被 libusb 接管后就没了对应的 sysfs 来源，但节点会永久留在
  容器里、打开必然报 ENODEV，还让 HID 驱动每轮探测失败、日志持续刷屏。现在节点跟随
  sysfs 回收：连续 3 轮在 sysfs 里查不到才删除（留 3 轮是为了不被重新枚举时的闪断误伤）。
- **修正文档里一处危险误导**：`dry_run` 是历史遗留字段、策略早已不引用；此前注释写着
  「dry_run=True 时只记录不执行」，实际上唯一的总开关只有 `enable_shutdown`。
- **清理失效的设备节点（幽灵节点）**。设备重插时按 sysfs 补齐出来的 `/dev/hidraw*`、
  `/dev/usb/hiddev*` 在设备被 libusb 接管后就没人管了，节点永久留着、打开必报 ENODEV，
  还让 HID 驱动每轮探测失败、日志持续刷屏。现在节点跟随 sysfs 回收：连续 3 轮在 sysfs
  里查不到就删除（留 3 轮是为了不被重新枚举时的瞬间闪断误伤）。
- **修正文档一处危险误导**：`dry_run` 是历史遗留字段，策略早已不引用它；此前注释写着
  「dry_run=True 时只记录不执行」，实际唯一的总开关只有 `enable_shutdown`。

### 新增

- 自愈相关配置项：`devnode_sync` / `nut_restart_min_interval` / `nut_restart_wait` / `lost_grace_sec` / `settle_sec` / `nut_ups_name`。
- `/api/status` 增加 `selfheal` 段（是否抖动中 / 已重拉次数 / 已补齐节点数 / 最近错误）；面板自愈期间显示「恢复中」，重连后显示「读数校准中」。
- `tools/test_selfheal.py` 补充设备节点垃圾回收用例（含「重枚举闪断不得误删」）。
- `tools/test_selfheal.py` 补充设备节点垃圾回收用例（含「重枚举闪断不得误删」）。
- `tools/test_selfheal.py` 补充设备节点垃圾回收用例（含重枚举闪断不得误删）。

### 验证

- 宿主级真实拔插（对 USB 设备做 unbind / bind，非容器内模拟）：拔断后 `upsc` 立刻报
  `Driver not connected`，引擎进入自愈；插回后自动补齐节点、重拉 NUT 栈，**31 秒恢复完整读数**。
- 整个 2 分钟断连期间**没有产生任何「UPS 通讯丢失」告警**（宽限期生效），这是和 v0.1.3 最大的差别。

### 验证

- 宿主级真实拔插（对 USB 设备做 unbind / bind，非容器内模拟）：拔断后 `upsc` 立刻报
  `Driver not connected`，引擎进入自愈；插回后自动补齐节点、重拉 NUT 栈，**31 秒恢复完整读数**。
- 整个 2 分钟断连期间**没有产生任何「UPS 通讯丢失」告警**（宽限期生效），这是与 v0.1.3 最大的差别。

### 验证

- 宿主级真实拔插（对 USB 设备做 unbind / bind，非容器内模拟）：拔断后 `upsc` 立刻报
  `Driver not connected`，引擎进入自愈；插回后自动补齐节点、重拉 NUT 栈，**31 秒恢复完整读数**。
- 整个 2 分钟断连期间**没有产生任何「UPS 通讯丢失」告警**（宽限期生效），这是与 v0.1.3 最大的差别。

## v0.1.3 — 2026-09-22

### 修复

- **电量读不到时不再按 0% 参与关机判据**。此前 `charge_pct` 缺失会被当成 0，一次 `upsc` 读取抖动就可能直接落进「电量低于阈值」触发预关机；现在缺失即跳过电量判据并记一条提示事件。
- **输出功率改画在独立瓦特轴上**。设备不上报 `ups.load` 时曲线回退显示「输出功率 W」，但纵轴仍是 0–100%，一条 25 W 的曲线只能贴在图底 —— 观感上等于「负载没动静」。现在瓦特轴独立，满轴 40 W。
- 容器挂了宿主根（`/host`）时 `shutdown_command` 会自动 `chroot` 进宿主执行（此前判据写反，默认命令以 `/` 开头，chroot 从未生效）。

### 新增

- **UPS 报告 FSD（`ups.status` 含 FSD，即即将断电）时纳入关机流程并落库**。此前这类状态被早退分支丢掉，UPS 主动通知等于没人听。

### 变更

- 设备不上报 `ups.load` 时（如 WalleCube openUPS HID），按 `输出电压 × 输出电流` 推算输出功率。
- `app/run.sh` 首启兜底配置 `driver_preferred` 修正为 `nut`（与主程序默认一致）。

## v0.1.2 — 2026-09-21

### 新增

- 安装 / 更新时自动去掉铁牛通知「测试消息」前缀：等长补丁系统 `TestMsg` 模板（自动备份、幂等、失败不阻断安装）。
- 手动补丁 / 还原脚本 `tools/patch_msg_prefix.sh`（系统升级还原二进制后重跑即可）。
- Docker 独立部署包：`docker/` 目录（Dockerfile + docker-compose.yml），NUT 在构建期装入镜像，任意 Linux Docker 主机可用。

## v0.1.1 — 2026-09-21

### 变更

- 默认走 NUT 桥接驱动（`driver_preferred=nut`），自研 HID 仅对 NUT 不识别的小众 UPS 兜底。
- NUT 容器内自包含集成（`apt install nut-server nut-client` + `upsdrvctl` + `upsd`）。
- `app/run.sh` 用 `user=root` + `upsd -u root` + `MODE=standalone` + `chmod 0666 /dev/hidraw*` 解决 NUT 权限。

### 修复

- `battery.runtime` ≥ 32000 识别为 NUT 占位值，面板显示「市电供电中」。
- 温度字段 fallback 到 `battery.temperature`。
- 事件 `detail` 带上 `ups.alarm` 字段。

### 新增

- 应用名定为「电源管理」/「Power Manager」（去掉旧称「UPS 电源管家」）。
- 面板对齐 fanctl 风格：深色默认 + 浅色可切（localStorage）、页脚署名、页头应用图标、主题切换按钮。
- 应用中心图标重做：深色青调渐变底 + 单色青 `#35c3d6` 线稿电池 + 闪电。

## v0.1.0 — 2026-09-20

### 新增

- 首个版本。
- 驱动抽象（MockDriver 模拟 / HidUpsDriver 自研 HID）+ SQLite 落库 + 断电策略引擎。
- 内嵌面板（状态卡 / 曲线 / 事件 / 设置 / 模拟控制台），口令 `admin` + token 鉴权。
- 铁牛通知栏推送（`common.msgcenter.services.Notice` + `Test.TestMsg` 模板）。
- 出厂双保险：`enable_shutdown=false` + `dry_run=true`。
