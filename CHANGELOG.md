# Changelog

电源管理 upsmgr 的版本更新记录。版本号格式：`v<主>.<次>.<修订>`。

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
