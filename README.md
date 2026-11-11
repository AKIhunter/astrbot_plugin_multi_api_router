<div align="center">

# 多 API 模型路由

![Banner](.github/img/Banner.png)

[![AstrBot](https://img.shields.io/badge/AstrBot-4.29%2B-50bda3)](https://github.com/AstrBotDevs/AstrBot)
![Version](https://img.shields.io/badge/Version-1.3.4-e9b949)
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
| 避开已经检测失败的 API | 健康检查确认的配置错误在有效期内自动跳过；真实调用成功会自动解除跳过 |
| 让不同错误得到一致处理 | 统一 `classify()` 输出类别与处置，不再按单个状态码各写一套分支 |
| 遵守上游的限流要求 | 429 优先按 `Retry-After` 等待；同一上游默认重试一次，仍失败才切换 |
| 防止偶发错误永久移除线路 | 连续失败只累计统计，不会暂停或移除线路 |
| 在所有上游失败后继续服务 | 整体模式可回退 AstrBot 主模型一次 |
| 让工具请求只走兼容模型 | 带工具时过滤未声明工具能力的上游 |
| 控制推理模型是否思考 | 每个上游独立设置，默认关闭 |
| 快速调整优先顺序 | 拖动整条 API，页面编号自动重排 |
| 限制整条降级链耗时 | 设置路由总超时，超时后停止后续 API 和回退 |
| 区分不同模型协议 | 为每个 API 标记聊天、生图、Embedding、语音等类型 |

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

取消“启用路由”后，该 API 不再参加整体自动路由，但配置不会停用。配置完整的
聊天、图转文和多模态 API 仍保留专属 Provider，可被其他插件直接指定使用。

## 两种 Provider 模式

| 模式 | Provider ID | 行为 | 适合场景 |
| --- | --- | --- | --- |
| 整体自动分配 | `multi_api_router` | 按页面顺序选择上游，支持健康跳过、失败切换和主模型回退 | 摘要、分类、预筛、文案等后台任务 |
| 指定某个上游 | 选择器显示上游名称和模型 | 只调用目标上游，不自动切换、不回退 | 视觉、翻译或必须固定模型的任务 |

每个配置完整的聊天兼容上游都会自动注册一个专属 Provider。其他插件的模型选择器会显示
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
| 路由超时限制 | 限制排队、逐个尝试、重试等待和主模型回退的总耗时 |
| 单次接口超时（秒） | 默认 45，范围 5–600；控制每次上游调用 |
| 最多尝试的上游数量 | 默认 3，范围 1–5；控制自动路由候选数量 |
| 每个上游异常重试次数 | 默认 1，范围 0–5；不含首次调用，0 表示不重试 |
| 异常重试间隔（秒） | 默认 1，范围 0–3600，支持小数；仅控制本轮重试等待 |
| 真实调用失败后回退主模型 | 所有上游真实调用失败后回退一次 |
| 回退 Provider ID | 留空使用默认主模型，也可指定固定 Provider |
| 自动跳过已确认的配置错误 | 仅跳过近期检测确认的 HTTP 401/402/403/404 |

修改后点击“保存并生效”即可热重载。重试等待计入路由总时限；
例如总时限为 90 秒、间隔设为 300 秒，本轮会在总时限耗尽时停止，
不会等满设定间隔，也不会冻结后续请求。实际重试次数可能小于配置上限。

### 单个上游

展开一条路由可以配置：

- 启用路由、API 类型、名称、API Base、API Key 和模型 ID
- 填写 API Base 和 API Key 后获取可见模型列表，或继续手动填写模型 ID
- 是否支持工具调用
- 是否启用模型思考能力
- 当前健康检查结果与专属 Provider ID

API 类型可选：聊天、文/图生图、图转文、Embedding、Rerank、STT、TTS、
ASR 和多模态。聊天、图转文、多模态使用 Chat Completions；其余类型不会被
错误送入聊天路由。生图和 TTS 仅手动检测，检测可能产生少量费用。

### 排序、保存与撤销

- 按住左侧三点拖动整条 API，上方线路优先调用。
- 页面编号按当前位置自动显示为 `#1`、`#2`、`#3`。
- 任意配置变化都会显示底部保存提示。
- 点击 **保存并生效** 会立即落盘并热重载。
- 点击 **取消并撤销** 或页面空白处，会恢复上一步输入、开关、新增、删除或排序。

右上角 **帮助手册** 包含每项配置的含义与常见搭配示例。

## 路由与降级逻辑

整体模式从入口到出口的链路是：预算检查 → 候选筛选 → 逐上游真实调用与错误处置 → 成功返回，或整体回退主模型/明确报错。具体顺序如下：

1. 分开检查文字与图片预算，超限时直接报错；旧图仅在发送副本压缩，原始历史保留。详见[输入预算与历史图片](INPUT_BUDGET.md)。
2. 只保留已启用路由、配置完整且兼容 Chat Completions 的上游。
3. 带工具请求只保留声明支持工具调用的上游。
4. 按 WebUI 从上到下的顺序排列候选。
5. 自动模式仅跳过近期健康检查确认的配置类错误（如 HTTP 401/402/403/404、400/422 或命中配额关键字）；超时、连接失败、429 与 5xx 的检测结果不会阻止真实调用。指定上游完全不受健康跳过影响。
6. 逐个真实调用；先由 `classify()` 得到 `(category, disposition)`，只有 `retry_then_rotate` 才在同上游重试，默认最多 1 次（`upstream_retry_count`，可设 0–5），任一 API 返回响应后立即停止。
7. 429 若带 `Retry-After` 就按该值等待，否则用固定重试间隔；两种等待都要先通过总预算校验。`rotate_now_report`（参数、认证、额度、路径类）立即切换，不占用重试次数。
8. 真实调用成功会通过 `health_recorder` 把该槽位写回健康，立即解除此前的健康跳过，不必等待有效期结束或手动检测。
9. 整条链路（含排队、重试、备用切换和回退）受 `route_timeout_seconds` 限制，指定上游也受该限制，超时后不再继续调用。
10. 连续失败只记统计，不冻结接口；下一条新请求仍可调用。旧 `failure_threshold` 和 `cooldown_seconds` 配置已停用。
11. 所有真实调用失败且仍有剩余时间时，整体模式可回退主模型一次。

需要工具但没有任何工具上游时，插件会明确报错，不会伪造工具执行结果。

### 错误分类与处置

所有上游错误先收敛到 `classify() -> (category, disposition)`。`category` 只用于日志和页面展示，`disposition` 决定路由动作，只有四个值：

| disposition | 含义 | 典型类别 | 路由动作 |
| --- | --- | --- | --- |
| `retry_then_rotate` | 上游暂时不可用 | 超时、连接错误、HTTP 408/429/5xx | 同上游最多重试一次，仍失败再切换 |
| `rotate_now_report` | 重试不会变好 | 认证失败、参数不兼容、路径错误、额度耗尽 | 立即切换并记录，不占重试 |
| `not_an_error` | 预期内的结果 | 撤回、上下文过期、策略取消、主动静默 | 不重试、不告警 |
| `fail_report` | 路由自身无法继续 | 无可用上游、指定上游缺失、工具上游缺失 | 直接报错 |

429 与 5xx 走同一条分类路径；只有响应体命中配额关键字（如 `insufficient_quota`、余额不足）时才改判为 `rotate_now_report`。`_retryable_error()` 不再维护独立规则，直接读取 `classify()` 的处置值，避免两套判断漂移。

HTTP 200 但正文为空且无工具调用不算路由错误：路由把它记为上游成功，同时打一条 `EMPTY_BODY_200` 锚点，是否按空响应重试由调用方决定。

### 429 限流留痕

命中 HTTP 429 时会打最多三段 WARNING，字段固定、单行可 grep：

| 事件 | 触发点 | 关键字段 |
| --- | --- | --- |
| `event=received` | 每次收到 429 | 槽位、上游名、模型、尝试序号、`error_type/code/message`、`Retry-After` 原值与秒数、`quota_hit` |
| `event=resolved` | 该 429 处理结束 | 是否重试、累计等待秒数、`retry_result`（success / failed / not_retried / budget_exhausted）、下一步动作 |
| `event=route` | 本次请求最终走向 | 尝试上游数、`outcome`（served_by / fallback / route_timeout / failed）与最终承接方 |

留痕只用于区分“限流”和“配额耗尽”，不参与重试判定、等待或切换，也不会改变健康状态（`health=unchanged_by_rate_limit`）。

### 故障锚点 `[ReplyFault]`

路由在“错误已经发生、但分类可能看不出来”的位置打单行 WARNING，字段为 `code`、`stage`、`category`、`disposition`、`slot` 与截断后的 `reason`。当前共 11 个调用点、9 个场景码：

| 场景码 | 触发条件 |
| --- | --- |
| `HEALTH_SKIP_CLEAR` | 真实调用成功，解除该槽位此前的健康跳过 |
| `PLAINTEXT_STATUS_UNPARSED` | 文案含 `HTTP 4xx/5xx`，但异常没有 `status_code`，只能按未知错误处理 |
| `RAW_CONN_UNKNOWN` | 连接类异常未命中连接分类，落到未知错误 |
| `BUDGET_VALUEERROR` | 输入预算/格式校验失败，抛出无状态码的 `ValueError` |
| `CONFIG_ERROR_DISPOSITION` | 无可用上游、指定上游缺失、或带工具但没有工具上游 |
| `EMPTY_BODY_200` | HTTP 200 但正文为空且无工具调用，已记为成功 |
| `HEALTH_SKIP_ALL_401` | 全部上游被健康跳过，且原因含 HTTP 401 |
| `HEALTH_SKIP_ALL` | 全部上游被健康跳过（其他状态码） |
| `AGGREGATE_DROP_QUOTA` | 配额类失败被聚合为普通 HTTP 429，调用方可能误按限流重试 |

锚点只增加诊断信息，不改变重试次数、切换顺序或健康状态。

### 健康跳过与自动解除

- `skip_unhealthy_upstreams`（默认开启）控制自动模式是否跳过健康检查确认的配置错误；关闭后仍会检测和展示，但不据此拦截。
- `health_skip_ttl_seconds`（默认 21600 秒，范围 60–604800）是检测结果有效期。超过有效期后不再据此跳过，避免插件刚启动或长时间未复检时被旧结果误拦。
- 只有 `rotate_now_report` 的检测结果会参与跳过；超时、连接错误、429 与 5xx 只展示，不会冻结真实请求。
- 健康检测成功会直接覆盖该槽位的检测结果（`ok=true`）；任意一次真实调用成功也会通过 `health_recorder` 回写健康。两者都会让下一次自动路由立即解除健康跳过。写回失败只记日志，不会推翻已经成功的模型调用。
- 指定上游（`multi_api_router:upstream_N`）绕过全部健康跳过，保证固定选择时行为可预期。

### 健康检测的输出预算

页面健康检测给 `max_tokens=256`，而不是早期的 8。原因是思考型模型会先把预算花在推理内容上：8 token 时正文被截断，接口返回 HTTP 200 但内容为空，页面显示“响应为空”，实际模型可用。提高到 256 后，模型思考完仍有预算输出 `OK`，才能真正验证“端点可达且模型可用”。检测只验证可用性，不代表输出质量。

### 上游故障时会怎样

| 情况 | 插件行为 |
| --- | --- |
| 健康检查临时异常 | 展示检测结果，真实请求仍可尝试 |
| 健康检查确认配置类错误（如 HTTP 401/402/403/404、400/422 或配额关键字） | 自动模式按 `skip_unhealthy_upstreams` 跳过并记录 WARNING；指定模式仍可调用 |
| 真实调用超时、连接错误或 5xx | 同上游重试一次，仍失败则按模式切换或报错 |
| 真实调用 429 | 优先按 `Retry-After` 等待并重试一次；无该头用固定间隔；命中配额关键字直接切换 |
| 真实调用成功 | 写回健康并立即解除该槽位此前的健康跳过 |
| 连续真实调用失败 | 只累计统计，后续请求不冻结 |
| 健康结果过期 | 不再依据旧结果跳过 |
| 整体模式全部失败 | 按配置回退主模型一次 |
| 指定上游失败 | 不被健康跳过拦截；临时异常按配置重试，耗尽后返回错误，不自动换线路 |
| 达到路由总超时 | 立即停止后续上游和主模型回退 |

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
健康检查确认的认证、路径或额度错误会在 `health_skip_ttl_seconds` 有效期内让自动路由暂时跳过该线路；
连续真实调用失败只累计统计，不会暂停或删除上游配置。任意一次真实调用成功都会把该线路写回健康，
下一次自动路由立即恢复，不必等有效期结束。

**为什么日志里出现三段 `[RateLimit]`？**
它们是一次 429 的完整证据链：`received` 记录收到的限流与响应头，`resolved` 记录是否重试、等了多久、
结果如何，`route` 记录本次请求最终由谁承接。留痕不改变重试判定，也不改变健康状态。

**为什么健康检测显示“响应为空”，但模型实际能用？**
早期检测只给 8 个输出 token，思考型模型把预算花在推理上，正文被截断，接口仍返回 HTTP 200。
现在检测预算为 256 token，模型思考完仍能输出 `OK`；若页面仍显示为空，请先看状态码与响应体。

**为什么指定上游失败后没有自动切换？**
这是指定模式的设计边界。需要自动容错时请选择 `multi_api_router` 整体模式。

**关闭“启用路由”后为什么仍能直接选择这个 API？**
该开关只控制是否参加整体自动路由。固定选择是独立入口，便于临时移出自动链路
后继续做单 API 测试或专用任务。

**为什么 Embedding、TTS 等类型没有出现在聊天模型选择器？**
这些 API 不属于 Chat Completions，插件不会伪装协议。分类用于保存能力边界和
按真实端点健康检测；当前 AstrBot 聊天 Provider 只注册聊天、图转文和多模态。

**为什么带工具的请求直接报错？**
请确认至少一个已启用上游勾选“支持工具调用”，并且对应模型确实兼容
function calling。

**思考能力对所有模型都有效吗？**
不会。该开关写入 `enable_thinking=true/false`，只对识别该参数的模型生效。
普通模型或中转不支持时应保持关闭。

**为什么 Provider 条目在停用插件后消失？**
这些条目由插件运行时注入，停用或卸载时自动清理，属于正常行为。

**健康检查返回 HTTP 402 怎么处理？**
通常表示上游额度或余额不足。自动路由会在检测结果有效期内跳过该上游
（日志和页面会标注 HTTP 402）；充值、更换 Key 后重新检测即可恢复。
指定上游模式不受跳过影响，仍会真实调用。

## 兼容性与安全

- 需要 AstrBot `4.29.0` 或更新版本。
- 聊天路由需提供 OpenAI Chat Completions 兼容接口；其他分类按对应 OpenAI
  兼容端点检测。
- API Key 保存在 AstrBot 插件配置中，不会写入状态日志。
- 控制台接口默认只回显掩码（如 `sk-a1b2****cdef`）；未编辑密钥直接保存不会覆盖原值。查看明文需点击页面「显示」，且必须已登录控制台，每次查看都会写入插件数据目录的 `api_key_reveal_audit.jsonl`（记录用户、时间、上游与来源 IP，不记录 Key 本身）。
- 兼容开关 `expose_plain_api_keys` 默认关闭；开启后 `/upstreams/config` 恢复旧行为（返回明文），仅用于临时回滚。
- 不要把私聊、长期记忆或敏感数据发送给不受信任的免费 API。
- 免费额度、模型名称和工具兼容性以服务商控制台为准。

## 使用指令

| 指令 | 作用 |
| --- | --- |
| `/模型路由状态` | 查看上游调用、成功、失败统计与健康跳过状态 |
| `/模型路由检测` | 立即检测全部已配置上游 |
| `/模型路由绑定预览` | 查看自动绑定计划 |
| `/模型路由恢复绑定` | 恢复自动绑定前的配置 |

## 更新日志

当前版本：`v1.3.4`

- 错误分类统一为 `classify() -> (category, disposition)`，`_retryable_error` 完全委托分类结果。
- 同一上游默认最多重试 1 次；429 优先遵守 `Retry-After`，剩余总预算不足时直接切换。
- 新增 429 的 `received` / `resolved` / `route` 三段限流留痕，只用于区分限流与配额耗尽。
- 真实调用成功通过 `health_recorder` 写回健康，立即解除该槽位的健康跳过。
- 健康跳过只在 `health_skip_ttl_seconds` 有效期内生效，指定上游不被跳过。
- 页面健康检测输出预算由 8 提到 256 token，修复思考型模型“200 但正文为空”。

[完整更新日志](CHANGELOG.md)
- 接入健康检查跳过、连续失败暂停和主模型回退。
- 支持整行拖拽排序与自动编号。
- 新增上游默认启用，工具能力默认开启，思考能力默认关闭。
- 配置变化显示底部保存提示，支持撤销上一步操作。
- 帮助手册补充全局设置、故障行为和常见搭配示例。

完整版本记录请查看 [CHANGELOG.md](CHANGELOG.md)。

如果这个插件对你有帮助，欢迎在 GitHub 点一个 Star。
