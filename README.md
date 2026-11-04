# 多 API 模型路由（astrbot_plugin_multi_api_router）

把多个 OpenAI 兼容 API 组合成一个 AstrBot Provider：按优先级自动挑一个能用
的上游，失败自动换下一个，连续失败只暂停不删除，真实调用全失败才回退主模型。

插件会在 AstrBot 的 Provider 列表里注册两种可选入口：

| 模式 | Provider ID | 说明 |
| --- | --- | --- |
| 整体模式 | `multi_api_router` | 插件内部按优先级自动分配上游，调用方不用关心路由细节 |
| 指定上游模式 | `multi_api_router:upstream_<编号>` | 只走某一个上游，不自动分配、不回退 |

## 1. 安装与打开

1. 把插件目录放到 AstrBot 的 `data/plugins/` 下，重启 AstrBot。
2. 在「插件管理」中找到「多 API 模型路由」，确认已启用。
3. 打开插件页面「上游配置」，填写 API Base、API Key、模型 ID，勾选启用并保存。

保存后插件会热重载，不需要重启 AstrBot。

## 2. 两种使用模式怎么配

### 整体模式（推荐给后台任务）

在 AstrBot 控制台的「服务提供商」里选择 `multi_api_router`（显示名「多 API
路由」）。之后所有走这个 Provider 的请求都由插件决定实际调用哪个上游。

适合：摘要、分类、候选预筛、文案生成、图片描述等后台任务。

### 指定上游模式（推荐给需要固定模型的场景）

每个已启用的上游都会自动生成一个 Provider：

```text
multi_api_router:upstream_1
multi_api_router:upstream_2
multi_api_router:upstream_3
...
```

在 AstrBot 的 Provider 选择器里直接选它，请求就只会发到这个上游。该模式不
自动降级、不回退主模型，行为与直接配置一个普通 OpenAI Provider 一致。

适合：视觉模型、翻译模型、需要固定模型名的插件。

> Provider 条目由插件在运行时注入，不会写进 `cmd_config.json`；停用某个上游后
> 对应条目会在下次保存/重载时自动消失。

## 3. 其他插件怎么调用

对调用方来说，它就是普通的 AstrBot Chat Provider，接口和参数与原生 Provider
完全相同，不需要知道内部路由：

```python
# 方式一：按 ID 取整体路由
provider = context.provider_manager.get_provider_by_id("multi_api_router")

# 方式二：取某个指定上游
provider = context.provider_manager.get_provider_by_id(
    "multi_api_router:upstream_2"
)

# 方式三：沿用 AstrBot 默认选择逻辑（把插件设为主模型时）
provider = context.get_using_provider(ProviderType.CHAT_COMPLETION)

resp = await provider.text_chat(
    prompt="用一句话总结这段内容",
    contexts=[],
    system_prompt="你是一个简洁的助手",
    func_tool=None,
)
print(resp.completion_text)
```

带工具调用同样不需要改调用方式，直接传 `func_tool` 即可。

扩展上游、调整优先级、增加降级策略都在插件页面完成，调用方代码不用动；也不
需要重新发版调用方插件。

## 4. 路由与降级逻辑

一次请求的处理顺序：

1. **输入预算**：超过 `max_input_chars` 直接报错，不调用任何上游。
2. **工具能力过滤**：请求带工具时，只考虑勾选了「支持工具调用」的上游。
3. **按优先级排序**：`priority` 数值越小越先尝试（相同则按编号）。
4. **健康检查跳过**：最近一次检测明确不通、且结果仍在有效期内的上游跳过并写
   日志；跳过不算真实失败。
5. **真实调用**：每个上游最多尝试一次，失败后换下一个，最多尝试
   `max_attempts` 个。
6. **连续失败暂停**：连续失败达到 `failure_threshold` 次，该上游暂停
   `cooldown_seconds` 秒（熔断），冷却结束自动恢复；**不会删除上游**。
7. **回退主模型**：只有真实调用全部失败才回退一次到主模型（整体模式）。
8. **明确失败**：需要工具但没有任何支持工具的上游时，直接报错说明原因，不会
   假装执行了工具。

## 5. 上游故障时会怎样

| 情况 | 行为 | 是否影响调用方 |
| --- | --- | --- |
| 健康检查发现不通 | 路由时跳过，写 WARN 日志 | 请求自动走下一个上游 |
| 单次调用失败 | 记一次失败，立即换下一个 | 无感切换 |
| 连续失败到阈值 | 暂停该上游一段时间，告警日志 | 无感切换 |
| 所有上游真实调用失败 | 整体模式回退主模型一次 | 可能拿到主模型结果 |
| 所有上游真实调用失败且关闭回退 | 抛出明确错误 | 调用方需要处理异常 |
| 指定上游模式失败 | 直接抛错，不换上游、不回退 | 行为可预期 |
| 健康检查结果过期 | 不再据此跳过 | 避免旧结果误拦请求 |

## 6. 页面能做什么

插件页面「上游配置」只保留必要信息：

- 上游列表与启用状态、优先级顺序
- 每个上游的健康检查结果（手动检测 + 每小时自动复检）
- 新增/删除上游，修改 API Base、Key、模型、优先级、是否支持工具
- 当前生效策略摘要，以及两种模式的 Provider ID

请求超时、并发、输入输出预算等低频参数仍在 AstrBot 的「插件配置」里。

## 7. 常见配置示例

### 例 1：两个免费上游做后台任务

```text
上游 1
  名称: SiliconFlow-Qwen
  API Base: https://api.siliconflow.cn/v1
  模型 ID: Qwen/Qwen2.5-7B-Instruct
  优先级: 10
  支持工具调用: 勾选

上游 2
  名称: 备用中转
  API Base: https://example.com/v1
  模型 ID: gpt-4o-mini
  优先级: 20
  支持工具调用: 勾选
```

AstrBot 里把后台插件指向 `multi_api_router`；上游 1 不通会自动走上游 2。

### 例 2：指定某个视觉模型

```text
上游 5
  名称: 通义视觉
  API Base: https://dashscope.aliyuncs.com/compatible-mode/v1
  模型 ID: qwen3-vl-plus
  优先级: 45
  支持工具调用: 勾选
```

需要视觉能力的插件选择 `multi_api_router:upstream_5` 即可，不会跑到别的上游。

### 例 3：上游不支持工具

如果某个中转不支持 function calling，在该上游详情里取消「支持工具调用」。
带工具的请求会自动跳过它，纯文本请求仍然可以使用。

## 8. 命令

- `/模型路由状态`：查看各上游状态、成功/调用次数、熔断与健康跳过情况。
- `/模型路由检测`：立即检测全部已配置上游。
- `/模型路由绑定预览`：查看自动绑定计划。
- `/模型路由恢复绑定`：恢复自动绑定前的配置。

状态与日志都不会显示 API Key。

## 9. 安全提醒

- API Key 只保存在插件配置中，页面接口仅限控制台管理员使用。
- 不要把包含敏感私聊或长期记忆的任务发送给不受信任的免费 API。
- 免费额度与模型列表会变化，请以服务商控制台为准。
