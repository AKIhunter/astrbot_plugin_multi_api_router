# 待办：上游槽位删除后不可恢复（retired_slots 只增不减）

> 记录时间：2026-11-12
> 状态：**已修复**（commit 1f0e4e3，2026-11-14 发布）——「删除即退役」改为
> 「删除即释放」：编号不再永久占用，复用前动态扫描外部引用
> （`multi_api_router:upstream_N` 出现在宿主/插件配置中则拒绝复用并报
> `blocked_slots`），引用检查不可读时拒绝放行；旧 `retired_slots` 字段
> 首次保存后自动移除。回归见 `tests/test_save_safety.py`。
> 以下内容为修复前的历史分析，留作设计记录。
> 影响版本：astrbot_plugin_multi_api_router 1.3.4
> 性质：设计取舍带来的可用性问题，不是崩溃类 bug

---

## 一、现象

删除一个 API 上游之后，**那个槽位号被永久占用，不能再用**。

槽位用满之后，「新增上游」直接拒绝：

```
没有可用槽位：20 个编号均已占用或已退役（删除过的编号不会复用）
```

---

## 二、机制（源码位置，可直接跳过去看）

**删除时写入退役列表** — `main.py` 的 `api_save_upstreams`（约 3112 行）：

```python
if "delete_slots" in payload:
    for raw_slot in payload["delete_slots"]:
        slot = int(raw_slot)
        key = f"upstream_{slot}"
        if key in self.config:
            del self.config[key]
            # 删除即退役：编号不再复用，防止其他插件保存的旧
            # multi_api_router:upstream_N 静默指向一个新上游。
            retired = self._retired_slots()
            if slot not in retired:
                retired.append(slot)
            self.config["retired_slots"] = sorted(retired)
```

**新建时跳过退役号** — `views/upstreams/app.js` 的 `addUpstream()`：

```javascript
const retired = new Set((state.retired_slots || []).map(Number));
// 优先复用"配置里已存在但页面隐藏的空槽位"
const reusable = state.upstreams.find(
  (item) => !item.route_enabled && !item.configured
         && !addedSlots.has(item.slot) && !retired.has(Number(item.slot)),
);
// 否则从 1 到 MAX_UPSTREAMS 找最小空闲号
for (let slot = 1; slot <= MAX_UPSTREAMS; slot += 1) {
  if (!used.has(slot) && !retired.has(slot)) { nextSlot = slot; break; }
}
if (!nextSlot) { showToast(`没有可用槽位：${MAX_UPSTREAMS} 个编号均已占用或已退役`, true); return; }
```

**上限** — `main.py:47`：`MAX_UPSTREAMS = 20`

**关键**：`retired_slots` 只增不减，**面板没有提供任何清理入口**。

---

## 三、复现步骤

配置路径：`/var/lib/astrbot/data/config/astrbot_plugin_multi_api_router_config.json`

1. 打开面板 → 模型 API 路由 → 任一上游（假设是 slot 5）填好 API Base / Key / 模型
2. 点该上游的**删除**，保存
3. 看配置文件：
   ```json
   "upstream_5": { ... }        ← 消失
   "retired_slots": [5]         ← 新增
   ```
4. 点「新增上游」→ 分配到 6（或下一个空闲号），**不会是 5**
5. 把 1–20 号逐一删除（或删到只剩退役号）→ 再点「新增上游」→ 报错「没有可用槽位」

---

## 四、当前实例的实测状态（2026-11-12 03:40）

```
retired_slots        : null（还没删过任何上游）
upstream_1..20       : 全部存在
其中空壳（只有名字，无 api_base）：
    slot 6  qwen-image-3.0-pro
    slot 8  Backup API 7
    slot 15-20  Backup API 14..19    共 8 个
provider 列表        : 12 条 multi_api_router:upstream_N
```

**结论：槽位号已经用满。** 现在每删一个就永久少一个，删到一定数量后无法新增。

---

## 五、影响（按严重度）

### 1. 槽位号单调衰减

20 个号，删够 20 次就再也加不了新 API。想恢复只能手工改 JSON。

### 2. 其他插件的 provider_id 会悬空（**这是最疼的**）

其他插件（子代理 / BiliBot / LivingHello 等）保存的是字符串
`multi_api_router:upstream_12`。一旦该槽位被删：

- AstrBot 重启后 `provider` 列表里那条消失
- 该插件启动时报 `Provider adapter not found: multi_api_router(multi_api_router:upstream_12). Skipped.`
- **而且无法通过"重建同号槽位"来恢复**——12 号已退役，新建只会拿到别的号
- 结果：那个 agent 直接不可用，而报错信息完全没提"槽位被删了"

> 这条在 2026-11-12 02:53 的真实日志里出现过：
> `[ERRO] Provider adapter not found: multi_api_router(multi_api_router:upstream_12). Skipped.`
> 当时的原因是插件目录结构问题，但**删槽位会造成一模一样的报错**，排查时极易混淆。

### 3. 唯一恢复手段是手工改配置

```bash
# 关掉 AstrBot，编辑，再启动
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("/var/lib/astrbot/data/config/astrbot_plugin_multi_api_router_config.json")
d = json.loads(p.read_text(encoding="utf-8-sig"))
d["retired_slots"] = [s for s in (d.get("retired_slots") or []) if s != 12]
p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
```

没有 UI、没有审计、容易忘。

---

## 六、修复方向

「删除即退役」这个设计本身是**对的**——它能防止旧引用静默指向新上游。
问题不在语义，在于**没有出口**。三个方向：

### A. 提供「释放槽位」操作（推荐）

在面板上加一个入口，语义明确：

1. 管理员勾选「释放 slot N」
2. 系统先**扫描所有引用方**，列出来：
   - `cmd_config.json` → `provider[]` 里 `id == multi_api_router:upstream_N`
   - `cmd_config.json` → `subagent_orchestrator.agents[].provider_id`
   - `data/config/*.json` 里的 `provider_id` 字段（BiliBot / LivingHello / AtGuard …）
   - `data/plugins/*/` 下的插件配置
3. 如果还有引用 → 列出插件名，要求二次确认（或直接拒绝）
4. 确认后从 `retired_slots` 移除，写一条 WARNING 日志

这样既保留了安全语义，又给了出口。

### B. 软退役 + TTL

`retired_slots` 改成 `{"slot": 5, "retired_at": 1762...}`，超过 N 天自动可复用，
复用时打 WARNING。实现简单，但"多久算安全"说不清——引用方可能几个月后才启动。

### C. 提高上限 + 让调用方能感知

`MAX_UPSTREAMS` 从 20 提到 50/100，同时把 `retired_slots` 放进
`GET /upstreams` 的返回（**已经放了**），让调用方自己判断。

只治标，但成本最低。

### 推荐组合

**A + C**：C 立刻缓解（提到 50，几乎不会撞上限），A 提供正确的语义出口。
B 不推荐——TTL 到期后静默复用，正好是原设计要防的事。

---

## 七、顺带发现的两个问题

### 1. slot 12 实际是坏的（HTTP 502）

```
slot 12  便宜01的大肥鱼1017  deepseek-v4.1-flash-1017
  → HTTP 502 {"error":{"message":"Upstream access forbidden, please contact administrator"}}
```

配置完整、`enabled=true`、`router_nodes` 显示 OK，但**实际请求被上游拒绝**。
排查时容易误判成"插件有问题"。

对照实测（同一次探测，2026-11-12 03:40）：

| 结果 | 槽位 |
|---|---|
| HTTP 200 可用 | 1、2、3、7、9、11、14 |
| HTTP 502 | **12** |
| 未配置完 | 6、8、15、16、17、18、19、20 |
| 非 chat 类型（跳过） | 4、5、10、13 |

**子代理 `ops_assistant` 当前用的正是 slot 12** ⇒ 它现在无法完成任何任务。
建议切到 14（`LightVela DeepSeek` / `deepseek-v4-pro-202606`）或其他可用节点。

### 2. 配置完整 ≠ 服务可用

`_upstream_public_data` 返回的 `configured` 只检查
`api_base && api_key && model` 三个字段非空，**不发请求**。
所以 502 的节点在 UI 和 API 里都显示"正常"，直到真实调用才暴露。

如果要做真实可用性，应该用已有的 `health` 字段（面板「健康检测」会写），
并在 `retired_slots` 修复时一并把 health 结果放进 `GET /upstreams` 的返回里
（目前 `_upstream_public_data` 已经带 `health`，但只有经过检测才有值）。

---

## 八、修复时的回归清单

改完请验证这几条：

- [ ] 删除 slot N → `retired_slots` 含 N，新建不会拿到 N
- [ ] 释放 slot N → `retired_slots` 不含 N，新建能拿到 N
- [ ] 释放时若有其他插件仍引用 N → 被拦下并列出插件名
- [ ] 释放后 `provider` 列表不残留旧条目
- [ ] 20 个号全占满时，「新增」提示准确（区分"占用"和"退役"）
- [ ] 已退役号在 `GET /upstreams` 里可见（现在有 `retired_slots` 字段，确认前端用到）
