# KiraAI-qqbot-fullmsg-bridge-plugin/QQ官方bot兼容与增强补丁 v1.1.0

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/znq19/KiraAI-qqbot-fullmsg-bridge-plugin)

# — 让 QQ 官方机器人也能像 NapCat 一样聊天

> 如果你还在被这一行刷屏，这个插件就是为你写的：
>
> ```
> _parser unknown event group_message_create.
> ```

QQ 官方机器人在开启「接收所有消息」后，会把群里的**每一条**消息推给 bot，
但 `qq-botpy` 的解析表里压根没有 `group_message_create` 这个键，事件在
websocket 回调最外层就被丢掉了 —— 报错是它唯一的痕迹。

本插件把这条路彻底打通，并且顺手把官方 bot 的语义对齐到 NapCat：
**真昵称、真 @ 判定、去重、可逆**。

---

## 它做了什么

| 问题 | 处理 |
|---|---|
| `_parser unknown event group_message_create`，不 @ 机器人的群消息全部丢失 | 为 `botpy.connection.ConnectionState` 补上群全量消息解析器（类级 + 运行中实例级，双保险），事件转成标准 `KiraMessageEvent` |
| **发件人昵称是 32 位 OpenID**（原生实现 `nickname = user_id`） | 群消息 / @消息 / 单聊三条路径统一接管，昵称取事件自带的 `author.username`（真实 QQ 昵称） |
| 全量消息若照抄原生逻辑，`is_mentioned` 被写死 `True` → 所有消息都算被 @、疯狂刷屏 | 按 `mentions[].is_you` 判定，缺失时用机器人自身 id 兜底；@ 事件路径强制 True |
| QQ 会重复推送同一 `msg_id`（官方明说） | 按 `(群, msg_id)` 直接去重，窗口 180 秒 |
| 引用消息（`message_type=103`）解析不到 | 传原始 payload 给框架的 `_message_chain`，引用内容照常解析 |
| 事件偶尔没带用户名 | 自动记住见过的 OpenID→昵称（`identities.json`），零维护、改名自动跟随 |
| 官方适配器只能被动回复（5 分钟 / 每条最多 5 次） | 可选开启官方「主动消息」通道兜底 |
| 补丁改完收不回 | **全程可逆**：关掉 `enabled` 后自动还原成框架原生实现，不需要重启 |

**S 版 / Z 版聊天插件一行都不用改** —— 只要事件语义对齐，关键词唤醒、围观、
存在感、休眠、骚扰检测全部照常工作。

---

## 安装

1. 本目录 → KiraAI 的 `data/plugins/qqbot-fullmsg-bridge/`
2. 重启 KiraAI（或热重载插件）
3. 前置条件：QQ 官方机器人适配器已经能正常收 @ 消息

### 必须做的平台侧设置

手机 QQ → 目标群 → 设置 → 机器人设置：

- `机器人可获取的群聊消息范围` → **获取群内全部消息**
- （可选）`机器人主动在群聊内发言` → 开（只有要用主动消息通道才需要）

### 装好后看这两条日志

```
[QQBOT-BRIDGE] 已为 botpy.ConnectionState 注册 parse_group_message_create（这是 `_parser unknown event group_message_create` 的直接修复）
[QQBOT-BRIDGE] qqo: 桥接就绪（全量群消息 + @消息 + 单聊；昵称取真实 QQ 昵称）
```

第一条全量群消息还会打一条样本：

```
[QQBOT-BRIDGE] 首条全量群消息：@判定来源=is_you；昵称='小明'；mentions 原文=[...]
```

**请看一眼这条**：如果 `@判定来源=none` 但那条消息其实 @ 了机器人，
把 `mention_mode` 改成 `always` / `never` 按需取舍。

---

## 配置

### 基础设置

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | 开 | 桥接总开关。关掉后会把所有补丁**还原**成框架原生实现 |
| `mention_mode` | `auto` | 只影响「全量群消息」这条路径的 @ 判定：`auto` / `always` / `never` |
| `unify_at_messages` | 开 | 接管 @ 消息：**昵称修正**的关键开关 |
| `unify_direct_messages` | 开 | 接管单聊：同样修正昵称 |
| `at_grace_seconds` | 0 | **不用动**。只有当日志报出「全量+@ 双副本」（按官方文档不该发生）时才设 1.5~2：全量副本会等一会儿，@ 副本随后到达就让位。代价是每条群消息晚这么多秒 |
| `dedup_ttl` | 180 | 消息去重窗口（秒） |
| `remember_nicknames` | 开 | 自动昵称通讯录，零维护 |

### 主动消息通道（可选，默认关）

| 键 | 默认 | 说明 |
|---|---|---|
| `proactive_enabled` | 关 | 官方被动回复超时（群聊 5 分钟 / 每条最多 5 次）后，改用官方主动消息接口兜底 |
| `proactive_min_interval` | 0 | 同一会话最小间隔（秒），**0＝不限速**。被动回复路径不受它影响，只有超出被动窗口的主动发言才走这条通道 |

> 主动消息**没有本地条数上限**：官方本身就有配额（1000 条/群/天 + 单关系 20/qpm），超了它会返回错误、我们照实记录 —— 本地再设一道只会让对话被静默掐掉。日志里会打「今日第 N 条」方便你观察。

### 同一条消息会不会来两次？

官方文档说得很明确：

> 当机器人开启了「接收所有消息」功能后，**群里的每一条消息（不限于 @ 机器人）**
> 都会推送此事件。　——《群消息（全量模式）》

也就是说：**开了全量模式之后，连 @ 机器人的消息也走 `GROUP_MESSAGE_CREATE`**，
不再单独走 `GROUP_AT_MESSAGE_CREATE`。AstrBot 那个 issue 的现象也印证了这点 ——
只挂了 AT 处理器的适配器，在全量模式下连 @ 消息都收不到。
所以正常情况下**不会有重复**，插件也不需要任何等待。

即便如此，插件也不会让你吃亏：

1. **同事件重推** —— 官方明说「相同 msg_id 可能多次推送」，这一层按 `(群, msg_id)` 去重（180 秒窗口）；
2. **万一跨事件重复**（文档没承诺过）—— 策略是「**绝不丢唤醒**」：@ 副本照常放行，
   同时打一条告警并计数，让你第一时间知道文档和现实对不上；
3. **想彻底消除** —— 把 `at_grace_seconds` 设为 1.5：全量副本先压住，@ 副本随后到达就让位。
   代价是每条群消息晚 1.5 秒，所以默认关闭。

> 停止日志里的「其中 N 条出现『全量+@』双副本，通常为 0」就是这条观测线。
> N 一直是 0 ⇒ 一切与文档一致，零开销。

---

## 与各聊天插件的兼容性

本插件只做「把官方 bot 的事件对齐成标准语义」，**不碰聊天逻辑**，所以 S 版、Z 版和
KiraAI 自带的默认聊天插件都能直接用。三者的唤醒判定都只依赖
`event.is_mentioned` + `message.chain` 里的 `Text`，这些字段现在都是对的。

| 能力 | 默认聊天（`default-chat`） | S 版 / Z 版 |
|---|---|---|
| 群内 @ 唤醒 | ✅ | ✅ |
| **群内关键词唤醒（不 @ 也能唤醒）** | ✅ **靠本插件才成立** | ✅ **靠本插件才成立** |
| 接收未提及消息（围观上下文） | ✅（需把 `receive_unmentioned` 打开，默认关） | ✅（`receive_unmentioned` 默认开） |
| 群聊主动发言 | ✅（依赖上一条） | ✅ |
| 单聊 | ✅ | ✅ |
| 真实 QQ 昵称而非 OpenID | ✅ | ✅ |

**「靠本插件才成立」是什么意思？** 默认聊天插件的说明写着
「如果消息中包含任一唤醒词，则视为被提及」——可是群里不 @ 机器人的消息，
在开启全量模式之前**根本不会推给机器人**，这个功能等于永远触发不了。
本插件把全量消息接进来之后，它才真的生效。同理，S 版 / Z 版的「非唤醒消息识别」
也需要先有非唤醒消息可收。

> 唯一的平台专有差异是 **戳一戳**：S 版 / Z 版有 poke 相关逻辑，在官方 bot 下会自然失效
> （官方平台没有 poke 事件，任何层都做不到）。这不影响其它任何功能。

---

## 必须知道的边界

- **戳一戳做不到。** QQ 官方机器人平台**没有** poke 事件、也**没有**发送 poke 的接口。
  botpy / KiraAI core / 插件任何一层都做不到「收到戳一戳并回戳」，这是平台能力缺失。
- **QQ 号拿不到。** 官方文档原文：「用户对象中所涉及的 ID 类数据，都**仅在机器人场景流通，
  与真实的 ID 无关**」。`openid` 是稳定主键（同一机器人 + 同一人永久不变），只是人不可读 ——
  本插件做的就是「内部用 openid，展示用真名」。
- **群名/群头像拿不到。** 群信息接口仅白名单；群成员列表/群成员信息接口官方标注
  「该能力正在内邀接入中」。
- 撤回他人消息、禁言、合并转发等 OneBot 能力，官方 bot 同样没有。

---

## 自测

```bash
python3 tests/run_tests.py
```

| 套件 | 覆盖 | 结果 |
|---|---|---|
| `tests/test_bridge.py` | 解析表补丁（含**真实 qq-botpy** 对照）、事件语义、昵称兜底、去重与双事件竞态、边界、能力降级、性能与内存、**可逆性** | **101/101** |
| `tests/smoke_real_core.py` | **真实 KiraAI core + 真实 qq-botpy + 真实 `QQOfficialAdapter`** 全链路：原始 payload → 真 `ConnectionState.parsers` → 真 `Client.ws_dispatch` → 真 `KiraMessageEvent`；含 100 条消息压测、「关闭后还原」与**跨事件重复观测**验证 | **37/37** |

```bash
# 冒烟需要真实源码路径（找不到会自动跳过）
KIRA_CORE=/path/to/kira_fw BOTPY_PATH=/path/to/botpy python3 tests/smoke_real_core.py
```

### 性能与阻塞

- `build_event` 实测 **≈ 6.5 µs/条**（含 payload dict 构造，20k 次取样）
- 消息路径上**没有同步 I/O、没有锁、没有无界循环**；昵称通讯录落盘走
  `asyncio.to_thread`，只在脏了的时候写
- 压测：一次性灌 100 条消息（60 全量 + 20 @ + 20 单聊）时，10ms ticker
  全程正常跳动，事件全部投递
- 内存有界：去重表 4096 条、昵称通讯录 4000 条封顶

---

## 更新日志

<details open>
<summary><b>v1.1.0</b> — 全面对齐 + 可逆</summary>

**昵称与语义对齐**
- @ 消息（`GROUP_AT_MESSAGE_CREATE`）与单聊（`C2C_MESSAGE_CREATE`）也统一接管：
  发件人昵称从 32 位 OpenID 改为事件自带的**真实 QQ 昵称**（原生实现是 `nickname = user_id`）
- 做法：覆盖 botpy 自带的 AT/C2C 解析器，让它们也把**原始 payload** 交给处理器
  （botpy 的消息对象会丢掉 `username` / `msg_elements` / `is_you`）；
  客户端实例上 shadow 原生 `on_*`，且**先挂处理器再换解析器**（顺序反了原生处理器会收到 dict）
- @ 事件按官方文档强制 `is_mentioned=True`（文档明写 AT 事件的 `mentions` **不含机器人自身**）

**竞态与去重**
- 按官方文档：开启全量模式后 @ 消息也走 `GROUP_MESSAGE_CREATE`，**不存在跨事件重复**，
  所以默认零等待；同事件重推按 `(群, msg_id)` 去重
- 万一真出现跨事件重复，按「绝不丢唤醒」放行 @ 副本 + 告警计数（观测线）
- `at_grace_seconds`（默认 0）保留为可选缓解开关；@ 副本先到时，随后的全量副本直接判重丢弃
- 去重表带 kind 的 LRU，`classify()` 返回 `new` / `dup` / `at_after_fm`

**可逆性（重要）**
- 新增 `restore_class_parser` / `drop_live_parser` / `detach_client_handler`：
  替换解析器时把原实现**存起来**，关闭插件或关掉某个 unify 开关后自动还原，
  **不需要重启进程**
- 修掉一个隐藏风险：只还原类级解析器、不还原运行中解析表的话，botpy 原生
  AT 处理器会收到 dict 而**静默丢消息**；现在两者一起还原，并覆盖了回归测试

**健壮性与性能**
- 目标适配器增加能力检查（缺 `_message_chain` 等接口时停用并给出清晰日志，
  而不是每条消息崩一次）
- `_is_allowed` / 回复锚点表 / `_remember_reply_id` 全部改为防御式读取，缺了不崩
- 昵称通讯录落盘移出消息路径（`asyncio.to_thread`，仅脏时写）
- 热路径日志改为惰性格式化；异常一律兜住，绝不抛回 botpy 的事件循环
- 新增性能 / 内存 / 事件循环存活性测试

**主动消息通道**
- `proactive_min_interval` 默认从 30 秒改为 **0（不限速）**：被动回复路径本来就不受它影响，
  30 秒只会误伤超出被动窗口的连续对话（群聊/私聊都是），默认不限速更合理

**命名与图标**
- 插件显示名定为「QQ官方bot兼容与增强补丁」，manifest 描述改成面向使用者的两句话
- 图标定为「唤醒」概念：白发少女 + 刚睁眼的机器人头 + 三道由灰到白的外扩唤醒光弧

</details>

<details>
<summary><b>v1.0.0</b> — 修复启动报错</summary>

- 修复 `_parser unknown event group_message_create`：为 `botpy.connection.ConnectionState`
  补上 `parse_group_message_create`（类级 + 运行中实例级）
- 把群全量消息接入 KiraAI 事件流：不 @ 机器人的消息可用于围观 / 关键词唤醒
- `is_mentioned` 按 `mentions[].is_you` 判定（缺失时回退比对机器人自身 id）
- 原始 `msg_id` 去重；昵称取 `author.username`；引用消息可解析

</details>

---

**让官方 bot 也能好好聊天，从这开始。**
