# auto-check-node

用于检查 Clash Verge Rev / mihomo 中指定地区代理节点的实时状态、目标 API 可达性，并可在当前节点达到策略确认条件时尝试切换到更优节点。默认地区为美国，保持旧版使用方式兼容。

脚本文件：

```text
check_us_proxy_status.py
```

详细需求见：

```text
docs/check_us_proxy_status_requirements.md
```

## 功能

- 通过 mihomo Unix Socket 调用控制 API。
- 从 `/proxies` 全量代理池读取代理对象。
- 本地按节点名称过滤指定地区真实节点，默认美国。
- 对每个地区节点执行基础延迟检测。
- 对一个或多个目标 API 执行可达性检测。
- 默认只检测状态，不主动切换任何策略组。
- 可显式启用自动切换：当前节点达到连续确认条件且未被冷却阻止时，扫描候选并尝试切到更优可用节点。
- 检测过程中保护策略组 `now`，发现非预期变化会尝试恢复。

## 环境要求

- Python 3.10+
- Clash Verge Rev / mihomo 已运行
- 控制 API 通过 Unix Socket 暴露，默认路径：

```text
/tmp/verge/verge-mihomo.sock
```

注意：`127.0.0.1:7890` 是系统代理流量端口，不是本脚本使用的控制 API 端口。

## 基本使用

输出人类可读结果：

```bash
./check_us_proxy_status.py
```

输出 JSON：

```bash
./check_us_proxy_status.py --json
```

指定检测地区：

```bash
./check_us_proxy_status.py --region sg --json
```

`--region` 支持 `us`、`sg`、`uk`、`jp`、`hk`、`de`、`fr`，大小写不敏感。默认 `us` 继续使用旧版美国关键词语义：`🇺🇸`、`美国`、`US`、`USA`、`United States`。

只检测基础状态，不加载默认 Discord 目标：

```bash
./check_us_proxy_status.py --no-default-targets
```

添加目标 API：

```bash
./check_us_proxy_status.py \
  --target discord=https://discord.com/api/v10/gateway \
  --target discord_cdn=https://cdn.discordapp.com
```

## 自动切换

自动切换默认关闭。启用后，脚本先只检测当前正在使用的指定地区节点；只有策略确认需要切换时，才扫描同地区候选节点。

```bash
./check_us_proxy_status.py \
  --region sg \
  --auto-switch-if-current-not-good \
  --switch-check-target discord
```

默认策略：

```text
good: 不切换，并重置计数器
slow: 低于或等于 600ms 时不切换；超过阈值且连续 5 次确认后才扫描候选
poor: 连续 2 次确认后扫描候选
dead / unknown / missing: 连续 2 次确认后扫描候选；冷却期内连续 dead 3 次可提前打破冷却
```

切换成功后默认进入 600 秒冷却期。冷却期内除连续 dead 达到打破条件外，只更新计数器，不执行新的切换。

候选节点必须同时满足：

- 属于当前策略组的 `all` 列表。
- 基础检测可用。
- 切换判断目标可用。
- 未命中近期切换过滤。
- 相比当前节点等级更好，或同等级延迟至少改善 100ms。

推荐命令：

```bash
./check_us_proxy_status.py \
  --auto-switch-if-current-not-good \
  --switch-check-target discord \
  --state-file logs/auto_switch_state.json \
  --bad-threshold poor \
  --bad-confirm-count 2 \
  --slow-switch-threshold-ms 600 \
  --slow-confirm-count 5 \
  --switch-cooldown-seconds 600 \
  --break-cooldown-dead-count 3 \
  --min-improvement-ms 100 \
  --confirm-candidate \
  --avoid-recent-switches 3 \
  --avoid-recent-window-seconds 1800
```

人类可读输出会显示决策原因、`bad` / `slow` / `dead` 计数器、冷却剩余时间、候选过滤数量、候选复测结果和最终切换状态。JSON 输出中对应字段为 `switch_policy`、`switch_decision`、`auto_switch.candidate_filter` 和 `auto_switch.candidate_confirmation`。

## cron 定时运行

可以用 cron 定时执行自动切换模式。建议在 cron 中使用绝对路径，并把输出写入日志文件。

编辑当前用户的 crontab：

```bash
crontab -e
```

每 5 分钟检查一次当前节点质量：

```cron
*/5 * * * * /usr/bin/python3 /path/to/auto-check-node/check_us_proxy_status.py --auto-switch-if-current-not-good --switch-check-target discord --json >> /path/to/auto-check-node/check_us_proxy_status.log 2>&1
```

每 1 分钟检查一次：

```cron
* * * * * /usr/bin/python3 /path/to/auto-check-node/check_us_proxy_status.py --auto-switch-if-current-not-good --switch-check-target discord --json >> /path/to/auto-check-node/check_us_proxy_status.log 2>&1
```

如果本机 `python3` 路径不是 `/usr/bin/python3`，先确认实际路径：

```bash
which python3
```

查看 cron 日志：

```bash
tail -f /path/to/auto-check-node/check_us_proxy_status.log
```

注意事项：

- cron 环境变量很少，命令里应使用脚本、Python、日志文件的绝对路径。
- Clash Verge Rev / mihomo 必须已经运行，且 Unix Socket 路径可访问。
- 自动切换只在当前节点达到连续确认条件且未被冷却阻止时扫描候选并尝试切换。
- 当前节点为 `good` 时，cron 每次运行都只检测当前节点，不会扫描其它候选节点。
- 如果担心日志持续增长，可以配合 `logrotate` 或定期清理日志文件。

## 延迟等级

```text
good    delay <= 300ms
slow    301ms <= delay <= 800ms
poor    delay > 800ms
dead    检测失败
unknown 检测成功但没有可用 delay 值
```

JSON 中 `base` 和每个 `targets` 检测结果都会包含 `level`：

```json
{
  "ok": true,
  "delay_ms": 183,
  "level": "good",
  "error": ""
}
```

## 常用参数

```text
--socket                       mihomo Unix Socket 路径
--region                       检测地区，支持 us/sg/uk/jp/hk/de/fr，默认 us
--url                          基础测速 URL
--timeout                      基础单节点测速超时时间，单位 ms
--target                       目标 API，格式 name=URL，可重复传入
--target-timeout               目标 API 单节点检测超时时间，单位 ms
--no-default-targets           不加载默认目标 API
--auto-switch-if-current-not-good
                               当前节点达到策略确认条件且未被冷却阻止时尝试自动切换
--switch-check-target          用于判断当前质量和选择最优节点的目标名
--state-file                   自动切换状态文件，默认 logs/auto_switch_state.json
--bad-threshold                进入 bad 确认逻辑的等级，目前固定支持 poor
--bad-confirm-count            poor/dead/unknown/missing 连续确认次数，默认 2
--slow-switch-threshold-ms     slow 延迟超过该值后才累计 slow 计数，默认 600
--slow-confirm-count           slow 超阈值后的连续确认次数，默认 5
--switch-cooldown-seconds      成功切换后的冷却时间，默认 600
--break-cooldown-dead-count    冷却期内允许提前切换的连续 dead 次数，默认 3
--min-improvement-ms           同等级候选需要达到的最小延迟改善，默认 100
--confirm-candidate            切换前复测选中的候选节点
--confirm-target               候选质量与复测目标，默认 switch 目标、discord、base
--avoid-recent-switches        近期切换过滤检查的事件数量，默认 3
--avoid-recent-window-seconds  近期切换过滤时间窗口，默认 1800
--prefer-groups                当前节点所属策略组优先级，逗号分隔
--json                         输出 JSON
```

## 退出码

```text
0  执行成功；未切换或只发生被允许的自动切换
1  运行错误，例如参数错误、Socket 不可用、API 返回异常
2  执行完成，但最终仍有非预期策略组状态未能恢复
3  自动切换被触发但执行失败或校验失败
```

## 验证

语法检查：

```bash
python3 -m py_compile check_us_proxy_status.py
```

真实环境验证：

```bash
./check_us_proxy_status.py --json
```

```bash
./check_us_proxy_status.py --region sg --json
```

```bash
./check_us_proxy_status.py \
  --no-default-targets \
  --target discord=https://discord.com/api/v10/gateway \
  --json
```

自动切换验证：

```bash
./check_us_proxy_status.py \
  --auto-switch-if-current-not-good \
  --switch-check-target discord \
  --json
```
