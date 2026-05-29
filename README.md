# auto-check-node

用于检查 Clash Verge Rev / mihomo 中美国代理节点的实时状态、目标 API 可达性，并可在当前节点质量不佳时自动切换到更优节点。

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
- 本地按节点名称过滤美国真实节点。
- 对每个美国节点执行基础延迟检测。
- 对一个或多个目标 API 执行可达性检测。
- 默认只检测状态，不主动切换任何策略组。
- 可显式启用自动切换：当前节点等级不是 `good` 时，扫描候选并切到更优可用节点。
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

自动切换默认关闭。启用后，脚本先只检测当前正在使用的美国节点。

```bash
./check_us_proxy_status.py \
  --auto-switch-if-current-not-good \
  --switch-check-target discord
```

触发规则：

```text
good: 不切换
slow: 尝试切换
poor: 尝试切换
dead: 尝试切换
missing: 尝试切换
```

如果当前节点仍有可比较延迟，候选节点必须比当前节点用于排序的延迟更低，避免切到更慢节点。

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
--url                          基础测速 URL
--timeout                      基础单节点测速超时时间，单位 ms
--target                       目标 API，格式 name=URL，可重复传入
--target-timeout               目标 API 单节点检测超时时间，单位 ms
--no-default-targets           不加载默认目标 API
--auto-switch-if-current-not-good
                               当前节点等级不是 good 时自动切换
--switch-check-target          用于判断当前质量和选择最优节点的目标名
--prefer-groups                当前节点所属策略组优先级，逗号分隔
--json                         输出 JSON
```

## 退出码

```text
0  执行成功
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
