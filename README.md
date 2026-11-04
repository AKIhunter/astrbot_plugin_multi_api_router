<div align="center">

# 多 API 模型路由

![Banner](.github/img/Banner.png)

[![AstrBot](https://img.shields.io/badge/AstrBot-4.29%2B-50bda3)](https://github.com/AstrBotDevs/AstrBot)
![Version](https://img.shields.io/badge/Version-1.3.2-e9b949)
[![Stars](https://img.shields.io/github/stars/AKIhunter/astrbot_plugin_multi_api_router?style=flat&color=e9b949)](https://github.com/AKIhunter/astrbot_plugin_multi_api_router/stargazers)
[![Last Commit](https://img.shields.io/github/last-commit/AKIhunter/astrbot_plugin_multi_api_router)](https://github.com/AKIhunter/astrbot_plugin_multi_api_router/commits/main)

</div>

把多个 OpenAI 兼容 API 组合成一个 AstrBot Provider。调用方继续使用
AstrBot 原生 Provider 接口，插件在内部完成排序、健康跳过、失败切换与主模型回退。

## 能做什么

| 你想做的事 | 多 API 模型路由如何处理 |
| --- | --- |
| 给后台任务准备多条模型线路 | 选择整体模式，按页面顺序自动尝试可用上游 |
| 固定使用某个模型 | 选择该上游的专属 Provider，不自动切换 |
| 避开已经检测失败的 API | 健康检查结果有效期内自动跳过并写日志 |
| 防止偶发错误永久移除线路 | 连续失败只暂停一段时间，冷却后自动恢复 |
| 在所有上游失败后继续服务 | 整体模式可回退 AstrBot 主模型一次 |
| 让工具请求只走兼容模型 | 带工具时过滤未声明工具能力的上游 |
| 控制推理模型是否思考 | 每个上游独立设置，默认关闭 |
| 快速调整优先顺序 | 拖动整条 API，页面编号自动重排 |

> **第一次使用？** 添加至少一个 OpenAI 兼容 API，填写 Base、Key 和模型 ID，
> 保存后在 AstrBot Provider 选择器中选 `multi_api_router` 即可。

## 目录

- [第一次使用](#第一次使用)
- [两种 Provider 模式](#两种-provider-模式)
- [WebUI 管理页面](#webui-管理页面)
- [路由与降级逻辑](#路由与降级逻辑)
- [其他插件如何调用](#其他插件如何调用)
- [常见问题](#常见问题)
- [兼容性与安全](#兼容性与安全)
- [使用指令](#使用指令)
- [更新日志](#更新日志)

## 第一次使用

1. 从 AstrBot 插件市场安装 **多 API 模型路由**，并重载插件。
2. 进入 **插件页面 → 多 API 模型路由 → 上游配置**。
3. 点击 **新增 API**，填写 API Base、API Key 和模型 ID。
4. 按住每条 API 左侧的三点拖动，调整实际调用顺序。
5. 点击底部 **保存并生效**，插件会热重载，不需要重启 AstrBot。
6. 在 AstrBot 或其他插件的 Provider 选择器中选择整体模式或指定上游。

新增 API 默认启用、默认声明支持工具调用、默认关闭思考能力。若上游不支持
function calling，请手动取消“支持工具调用”。

## 两种 Provider 模式

| 模式 | Provider ID | 行为 | 适合场景 |
| --- | --- | --- | --- |
| 整体自动分配 | `multi_api_router` | 按页面顺序选择上游，支持健康跳过、失败切换和主模型回退 | 摘要、分类、预筛、文案等后台任务 |
| 指定某个上游 | 选择器显示上游名称和模型 | 只调用目标上游，不自动切换、不回退 | 视觉、翻译或必须固定模型的任务 |

每个已启用上游都会自动注册一个专属 Provider。其他插件的模型选择器会显示
`qwen7b（Qwen/Qwen2.5-7B-Instruct）` 这类可读名称，不显示内部路由前缀。
内部仍使用稳定 ID 保存配置，例如：

```text
multi_api_router:upstream_1
multi_api_router:upstream_2
multi_api_router:upstream_5
```

Provider 条目由插件在运行时注入，不写入 `cmd_config.json`。停用上游或插件后，
对应条目会在保存、重载或终止时自动清理。

## WebUI 管理页面

页面保持左侧全局设置、右侧路由列表的双栏结构。

### 全局设置

| 配置 | 作用 |
| --- | --- |
| 启用路由 Provider | 临时启停整套路由 |
| 整体模式 Provider ID | 其他插件选择整体路由时使用的 ID |
| 真实调用失败后回退主模型 | 所有上游真实调用失败后回退一次 |
| 回退 Provider ID | 留空使用默认主模型，也可指定固定 Provider |
| 跳过健康检查不通的上游 | 在检测结果有效期内不调用已知故障线路 |

### 单个上游

展开一条路由可以配置：

- 启用状态、名称、API Base、API Key 和模型 ID
- 是否支持工具调用
- 是否启用模型思考能力
- 当前健康检查结果与专属 Provider ID

### 排序、保存与撤销

- 按住左侧三点拖动整条 API，上方线路优先调用。
- 页面编号按当前位置自动显示为 `#1`、`#2`、`#3`。
- 任意配置变化都会显示底部保存提示。
- 点击 **保存并生效** 会立即落盘并热重载。
- 点击 **取消并撤销** 或页面空白处，会恢复上一步输入、开关、新增、删除或排序。

右上角 **帮助手册** 包含每项配置的含义与常见搭配示例。

## 路由与降级逻辑

一次整体模式请求按以下顺序执行：

1. 检查输入长度，超过 `max_input_chars` 时直接报错。
2. 只保留已启用且配置完整的上游。
3. 带工具请求只保留声明支持工具调用的上游。
4. 按 WebUI 从上到下的顺序排列候选。
5. 跳过仍在有效期内的健康检查失败上游，跳过不计真实失败。
6. 逐个真实调用，每条线路最多尝试一次，最多尝试 `max_attempts` 个上游。
7. 连续失败达到 `failure_threshold` 后暂停该上游 `cooldown_seconds` 秒。
8. 所有真实调用失败后，整体模式可回退主模型一次。

需要工具但没有任何工具上游时，插件会明确报错，不会伪造工具执行结果。

### 上游故障时会怎样

| 情况 | 插件行为 |
| --- | --- |
| 健康检查不通 | 跳过该上游并记录 WARNING |
| 单次真实调用失败 | 记录失败并尝试下一条线路 |
| 连续失败达到阈值 | 暂停一段时间，配置不会删除 |
| 健康结果过期 | 不再依据旧结果跳过 |
| 整体模式全部失败 | 按配置回退主模型一次 |
| 指定上游失败 | 直接返回错误，不自动换线路 |

## 其他插件如何调用

对调用方来说，它就是普通的 AstrBot Chat Provider：

```python
from astrbot.api.provider import ProviderType

# 整体模式
provider = context.provider_manager.get_provider_by_id("multi_api_router")

# 指定某个上游
pinned_provider = context.provider_manager.get_provider_by_id(
    "multi_api_router:upstream_2"
)

# 跟随 AstrBot 当前默认 Provider
default_provider = context.get_using_provider(ProviderType.CHAT_COMPLETION)

resp = await provider.text_chat(
    prompt="用一句话总结这段内容",
    contexts=[],
    system_prompt="你是一个简洁的助手",
    func_tool=None,
)
print(resp.completion_text)
```

带工具请求继续使用原生参数，将 `ToolSet` 传给 `func_tool` 即可。新增上游、
调整排序或修改降级策略时，调用方代码不需要变化。

## 常见问题

**为什么上游显示健康检查失败，但没有被删除？**
健康检查失败只会让路由暂时跳过该线路。连续真实调用失败也只会触发冷却暂停，
不会删除上游配置。

**为什么指定上游失败后没有自动切换？**
这是指定模式的设计边界。需要自动容错时请选择 `multi_api_router` 整体模式。

**为什么带工具的请求直接报错？**
请确认至少一个已启用上游勾选“支持工具调用”，并且对应模型确实兼容
function calling。

**思考能力对所有模型都有效吗？**
不会。该开关写入 `enable_thinking=true/false`，只对识别该参数的模型生效。
普通模型或中转不支持时应保持关闭。

**为什么 Provider 条目在停用插件后消失？**
这些条目由插件运行时注入，停用或卸载时自动清理，属于正常行为。

**健康检查返回 HTTP 402 怎么处理？**
通常表示上游额度或余额不足。充值、更换 Key 后，在路由列表中重新检测即可。

## 兼容性与安全

- 需要 AstrBot `4.29.0` 或更新版本。
- 上游需提供 OpenAI Chat Completions 兼容接口。
- API Key 保存在 AstrBot 插件配置中，不会写入状态日志。
- 不要把私聊、长期记忆或敏感数据发送给不受信任的免费 API。
- 免费额度、模型名称和工具兼容性以服务商控制台为准。

## 使用指令

| 指令 | 作用 |
| --- | --- |
| `/模型路由状态` | 查看上游调用、成功、失败、熔断与健康跳过状态 |
| `/模型路由检测` | 立即检测全部已配置上游 |
| `/模型路由绑定预览` | 查看自动绑定计划 |
| `/模型路由恢复绑定` | 恢复自动绑定前的配置 |

## 更新日志

当前版本：`v1.3.2`

- 模型选择器改为显示上游名称和模型，不再暴露内部路由前缀。
- Provider 内部 ID 保持不变，已有插件配置无需迁移。
- 支持整体自动分配与指定上游两种 AstrBot Provider。
- 接入健康检查跳过、连续失败暂停和主模型回退。
- 支持整行拖拽排序与自动编号。
- 新增上游默认启用，工具能力默认开启，思考能力默认关闭。
- 配置变化显示底部保存提示，支持撤销上一步操作。
- 帮助手册补充全局设置、故障行为和常见搭配示例。

完整版本记录请查看 [CHANGELOG.md](CHANGELOG.md)。

如果这个插件对你有帮助，欢迎在 GitHub 点一个 Star。
