# instabox-keepalive

InstaBox 免费沙箱（机器 `bc`）的外部保活看门人：GitHub Actions 每 5 分钟运行一次，
解决官方两个免费档硬限制（服务端确认无法配置解除）：

| 限制 | 机制 | 对策 |
| --- | --- | --- |
| 闲置 30 分钟自动挂起 (`autoSuspendAfterSeconds=1800`) | 无操作即触发 | 每轮 `POST /wake` 重置闲置计时（沙箱内另有 busy 探针 + systemd 心跳双保险） |
| 单次运行 480 分钟硬顶 (`maxRunMinutes=480`，free 固定) | 连续运行到点强制暂停 | 已连续运行 ≥360 分钟时主动 `POST /suspend` 换气（保留内存与进程，工作无感），冷却后 `POST /wake`，双计时器归零，永远碰不到 480 分钟顶 |

官方规则：*Both timers restart on resume. Runtime pauses require a cooldown
before waking.* 且沙箱内部无法唤醒自己（suspend 后整机冻结）——所以唤醒器必须放
在沙箱外，这就是本仓库存在的意义。

## 设计

- **无状态幂等**：状态真值全部存在 InstaBox 机器对象（`state` / `runStartedAt` /
  `cooldownUntil`），任意一轮失败/重复执行都安全。
- 每 20 分钟（`9,29,49 * * * *`，刻意错开 GitHub 最拥挤的 `*/5` 档位以保证触发可靠）跑一轮
  `keepalive.py`：running→wake 心跳（或到 6 小时阈值→主动换气）；
  suspended 且冷却已过→wake 唤醒；stopped→start。
- 密钥存于仓库 Secret `INSTABOX_API_KEY`（scopes: `machines:read` + `machines:write`），
  不会出现在日志或代码里。

## 手动操作

- 手动跑一轮心跳：Actions → instabox-keepalive → Run workflow（refresh=false）
- 手动做一次完整换气测试：Run workflow（refresh=true）

沙箱内对应的自保层（busy 探针 + `instabox-watchdog.service` systemd 心跳）
部署在机器 `bc` 上：`/root/scripts/instabox_watchdog.py`。
