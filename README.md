# KiraAI-qqbot-fullmsg-bridge-plugin/QQ官方bot增强 v1.3.0

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

**一句话**：**同一份代码**，在 KiraAI 2.x 上是"补丁型增强"（核心缺的要补），
在 **KiraAI 3.0 上是"纯增强插件"**（核心已做的一律不碰，只做核心没做的）。
装完即用，**不需要改任何配置**。

## 版本定位（2.x / 3.0 自动适配）

| 核心 | 桥接的角色 | 说明 |
|---|---|---|
| **KiraAI 3.0** | **纯增强** | 3.0 已把「全量群消息 / 真昵称 / @ 解析 / 引用收发 / 去重 / 语音·卡片·表情归一化」全做完，桥接**绝不重复接管、绝不另造事件**，只补下面 6 项核心没做的 |
| **KiraAI 2.x** | **补丁型增强** | 核心缺全量群消息解析器、昵称写成 OpenID，桥接必须补上并接管 |
| 认不出世代 | 降级 | 只启用「群管理工具 + markdown/键盘 + 引用」这些不依赖核心内部结构的增强 |

> 世代靠**结构探测**（有没有 IMCapability、方法在 adapter 还是在能力对象上）判断，
> 不依赖版本号字符串，所以 KiraAI 以后改版本号也不会误判。

## 新增能力（2.x 与 3.0 都有，因为两家核心都没有）

| 能力 | 说明 |
|---|---|
| **群名** | 后台调官方接口把会话标题从群 OpenID 换成中文群名。接口是**白名单（内邀）**，拿不到就自动降级为 OpenID（**只提示一次**），**用户不需要在 QQ 那边做任何设置** |
| **markdown** | `<markdown>` 标签 → 模型能发富文本。官方 2026-04-23 起，单聊/群聊自定义 markdown **对所有机器人开放**，无需申请模板；失败自动退纯文本 |
| **内联按钮** | `<keyboard>` 标签 → 消息下方挂按钮；带完整 JSON 校验（行/列/长度），非法直接拒绝并告知模型 |
| **按钮点击回调** | 用户点按钮 → **3 秒内回执**（官方硬要求，否则客户端一直转圈）→ 转成一条消息给模型 |
| **群管理工具** | `recall_qq_msg`（撤回，2 分钟窗口）/ `set_qq_group_ban`（禁言·解禁，需群管理员）/ `get_group_mute_state` / `get_qq_bot_state`。**直接调官方接口，不依赖框架内部结构，跨版本可用** |
| **成员事件** | 有人进群/退群/申请加群 → 作为 System 消息告诉模型（需打开 `extra_intents`，默认关） |
| **3.0 引用唤醒补洞** | 3.0 判断"这条引用是不是在叫机器人"用的是**内存里自己发过的消息**，**重启后失效**。桥接改读平台下发的 `author.bot`，重启后依然成立 |


| 问题 | 处理 |
|---|---|
| `_parser unknown event group_message_create`，不 @ 机器人的群消息全部丢失 | 为 `botpy.connection.ConnectionState` 补上群全量消息解析器（类级 + 运行中实例级，双保险），事件转成标准 `KiraMessageEvent` |
| **发件人昵称是 32 位 OpenID**（原生实现 `nickname = user_id`） | 群消息 / @消息 / 单聊三条路径统一接管，昵称取事件自带的 `author.username`（真实 QQ 昵称） |
| 全量消息若照抄原生逻辑，`is_mentioned` 被写死 `True` → 所有消息都算被 @、疯狂刷屏 | 按 `mentions[].is_you` 判定，缺失时用机器人自身 id 兜底；@ 事件路径强制 True |
| QQ 会重复推送同一 `msg_id`（官方明说） | 按 `(群, msg_id)` 直接去重，窗口 180 秒 |
| 引用消息（`message_type=103`）解析不到 | 传原始 payload 给框架的 `_message_chain`，引用内容照常解析 |
| 事件偶尔没带用户名 | 自动记住见过的 OpenID→昵称（`identities.json`），零维护、改名自动跟随 |
| 官方适配器只能被动回复（5 分钟 / 每条最多 5 次），**msg_id 过期（40034005）直接丢消息** | 自动改走官方「主动消息」通道兜底，并清掉死 id |
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

> 新增的 7 项（群名 / markdown / 键盘 / 互动 / 群管理工具 / 成员通知 / 额外订阅位）
> **除 `extra_intents` 外全部默认开**，一般不需要动。

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
| `resolve_at_markup` | 开 | 把 `<@openid>` 解析成标准 `At` 元素 `[At 昵称(pid)]`（**保留 pid 防改名冒充**）；机器人自己被 @ 时名字带「（你）」并强制唤醒 |
| `learn_self_openid` | 开 | 自动学出机器人自己的 OpenID（优先 `mentions[].is_you`，兜底用"查不到的 @"反推） |
| `reply_to_self_wakes` | 开 | **有人引用回复机器人的消息 = 被提及**（对齐 KiraAI 的 OneBot 适配器行为） |
| `quote_reply` | 开 | **机器人也能「引用回复」** —— 官方发送接口支持 `message_reference`，填上就以引用形式展示 |
| `send_at_mention` | 开 | **机器人发出的 @ 是真 @**（而不是纯文本 `@昵称`） |
| `at_markdown` | **开** | **含 @ 标记的正文自动改走 markdown 消息**（重要：纯文本消息没有 @ 能力，见下文） |
| `at_markup_style` | **legacy** | @ 标记形态：`legacy` = `<@openid>`（**默认**，平台自己下发用的那种）/ `new` = `<qqbot-at-user id="…" />`（官方文档推荐，但实测部分环境会被当纯文本原样显示） |
| `enhance_rich_content` | 开 | **富内容归一化**：语音（含平台免费 ASR）/ 结构化卡片 / QQ 表情标记 → 都能读 |
| `self_openid` | 空 | 通常留空＝全自动。只有自动识别猜错时才把它钉死 |
| `group_name_enabled` | 开 | 后台把会话标题从群 OpenID 换成中文群名。该接口是**白名单（内邀）**，拿不到就自动降级为 OpenID（只提示一次），**用户不需要在 QQ 那边做任何设置** |
| `markdown_enabled` | 开 | 注册 `<markdown>` 标签，让模型能发富文本（官方 2026-04-23 起自定义 markdown 对所有机器人开放，无需申请模板） |
| `keyboard_enabled` | 开 | 注册 `<keyboard>` 标签，让模型能在消息下挂内联按钮 |
| `interaction_enabled` | 开 | 接收按钮点击（INTERACTION_CREATE）：**3 秒内回执** + 转成消息给模型 |
| `admin_tools_enabled` | 开 | 4 个官方能力工具：撤回 / 禁言解禁 / 禁言查询 / 机器人群内状态 |
| `member_notice_enabled` | 开 | 成员进出/加群申请作为 System 消息告诉模型（需 `extra_intents`） |
| `extra_intents` | **关** | 额外订阅「成员事件 1<<24 + 互动回调 1<<26」。官方平台事件订阅是硬要求，不订就收不到；**默认关**是因为个别环境多订阅会导致连接反复失败，打开后需**重启 KiraAI**才生效 |

### 主动消息通道（默认开）

| 键 | 默认 | 说明 |
|---|---|---|
| `proactive_enabled` | **开** | 官方被动回复失效（超时 / 40034005「msg_id已过期」）后，改用官方主动消息接口兜底；命中过期时顺手清掉死 id，避免后续每条都白失败一次 |
| `proactive_min_interval` | 0 | 同一会话最小间隔（秒），**0＝不限速**。被动回复路径不受它影响，只有超出被动窗口的主动发言才走这条通道 |

> 主动消息**没有本地条数上限**：官方本身就有配额（1000 条/群/天 + 单关系 20/qpm），超了它会返回错误、我们照实记录 —— 本地再设一道只会让对话被静默掐掉。日志里会打「今日第 N 条」方便你观察。

### LLM 认不出"有人在叫我"？—— @ 富文本的锅

官方 bot 群消息里的 @ 是**富文本标记**，直接留在 `content` 里：

```
<@0A0B9F323E6AA18BF08B6901A3B2DEFC> 妹
```

KiraAI 原生实现完全不解析它（`mentions` 数组也没用上），所以 LLM 看到的是一串
32 位 hex —— **连"这是在叫我"都判断不出来**，只能当成"哥在 @ 别人"。

插件把它解析成 **KiraAI 标准 `At` 元素**，渲染出来是：

```
[At 香里（你）(0A0B9F323E6AA18BF08B6901A3B2DEFC)] 妹
```

#### 为什么一定要保留 pid，不能只显示昵称

**昵称是用户随时可以改的字段。** 只显示昵称的话，任何人把群昵称改成「香里」就能冒充机器人
—— LLM 分不出谁是谁。而 `pid` 是平台下发的 openid，**用户不可控**。

KiraAI 原版就是这个约定：`At` 的渲染是 `[At 昵称(pid)]`，OneBot 路径甚至只给
`[At QQ号]`（只有 id、没有名字）—— **名字给人读，pid 做身份**。

机器人自己的那个 `At`，名字额外带一个「**（你）**」后缀（`[At 香里（你）(pid)]`），
这样"同名冒充"在语义上也一眼分得清：真身带「（你）」，冒充者不带。

插件做了三件事：

1. **认出"自己"**：优先用事件里的 `mentions[].is_you`；没有就用「`bot=true` 且昵称与机器人
   名字一致」比对；都没有时兜底反推 —— 内容里有、`mentions` 里查不到的 `@`，那就是机器人自己
   （平台本来就会把机器人从 `mentions` 里摘掉）。
2. **拆成标准 At 元素**：`<@openid>` → `At(pid=openid, nickname=昵称)`。**pid 永远保留**；
   名字只是锦上添花，解析不出来时给 `[At pid]`，绝不编造身份。自己的 `At` 名字带「（你）」。
3. **硬唤醒**：内容里出现"自己的 @"就**强制** `is_mentioned=True`，这条证据比 `mentions`
   更硬，不依赖平台给不给 `is_you`。

> 第一次遇到 @ 富文本时，日志会打出原文 / 解析结果 / mentions / 认出的 OpenID 及其来源，
> 方便你一眼核对：
> ```
> [QQBOT-BRIDGE] 首次遇到 @ 富文本：原文='<@0A0B...> 妹' → 解析后='@香里 妹'；mentions=[...]；机器人 OpenID=0A0B...（来源 is_you）
> ```

### @ 他人 和 引用回复 也对齐了吗？

**@ 他人：一样处理。** 一条消息里 @ 了几个人就拆出几个 `At`，各自带自己的 pid 和昵称：

```
小红: <@AAAA1111BBBB2222> 你看 <@CCCC3333DDDD4444>
                                        ↓
小红: [At 小红(AAAA1111BBBB2222)] 你看 [At 小刚(CCCC3333DDDD4444)]
```

只 @ 别人**不算唤醒**（`is_mentioned` 保持 False）——只有真被 @ 才唤醒，和 NapCat 一致。

**引用回复：内容能解析出来，而且「被引用回复」= 被唤醒。**

先说唤醒这一条 —— 这是对齐 KiraAI 的 QQ(OneBot) 适配器：

```python
# core/adapter/src/qq/qq.py
elif m.get("type") == "reply":
    reply_msg_info = await self.bot.get_msg(...)
    if reply_msg_info["data"]["user_id"] == msg["self_id"]:
        is_mentioned = True        # ← 「回复机器人自己的消息」在框架里就等于被提及
```

本插件照做。而且 QQ 官方这边**比 OneBot 还省事**：被引用消息的作者直接就在
`msg_elements[0].author` 里（`bot: true` + 名字），不用像 OneBot 那样额外发一次 `get_msg`
去反查。引用别人的消息则**不算**唤醒。

**机器人自己也能引用回复了。** 官方发送接口本来就有这个能力（文档原文：
`message_reference` —— 「**引用回复。填写后以引用形式展示，关联上下文**」），
KiraAI 只是没传这个字段。所以不需要任何额外连接，只是多发一个字段：

- 机器人要引用的那条消息 → 用官方要求的 **`REFIDX`**（不是消息 id）：
  - 别人发的消息：取事件 `message_scene.ext` 里的 `msg_idx`
  - 机器人自己发的消息：取发送响应 `ext_info.ref_idx`
- 只有当机器人**主动引用**某条消息（LLM 输出了 `<reply>xxx</reply>`）时才带上 ——
  否则每条消息都会变成"引用上一条"，那是刷屏。

再说内容解析 —— 这是**原生实现做不到的**。

KiraAI 原生适配器会先把事件包装成 botpy 的 `GroupMessage` 对象，而那个对象**只保留
`content` / `mentions` / `attachments`** —— 引用消息靠的 `msg_elements` 被丢掉了，
所以原版下引用内容根本进不来。本插件把**原始 payload** 交给框架的 `_message_chain()`，
于是：

- `message_type=103` 的引用消息 → 生成标准 **`Reply` 元素**，被引用的原话作为它的 `chain` 带进来；
- S 版 / Z 版的**「引用检测」**（骚扰判定的 `_detect_kind` → `"reply"`）依赖真正的
  `Reply` 元素 —— 现在有了，这条路径才真正生效；
- **引用内容里的 @ 也会拆成 `At`**，但**不算"现在在叫我"** —— 那是被引用的历史消息，
  不是当前这条消息在呼唤你（避免被"引用一条 @ 过机器人的旧消息"误触发）。

> **已知边界**：QQ 群消息的 payload 里**没有** `message_reference`（引用指针在
> `message_scene.ext` 的 `ref_msg_idx`），所以 `Reply` 的 message_id 是空的，渲染成
> `[Reply ]` + 引用内容 —— **内容有、id 没有**。若某条消息 QQ 连 `msg_elements` 都没给，
> 那就拿不到引用信息（退化成普通文本）。
> 没有把 `ref_msg_idx` 塞进 `message_reference` 是**故意的**：那是索引不是消息 id，
> 塞进去会让"回复它"变得可点击、最终以非法 msg_id 发送失败。

### 机器人 @ 人，也是真 @ 吗？

**是** —— 但要用平台认的标记，纯文本不行。官方《文本交互》文档给了**两种**写法：

| 形态 | 写法 | 情况 |
|---|---|---|
| **new** | `<qqbot-at-user id="openid" />` | 文档推荐；**但实测在你那边被当纯文本原样显示了** ❌ |
| **legacy**（默认） | `<@openid>` | 文档标注"即将弃用"，**可它正是平台自己下发给我们**的形态（入站 content 里的 @ 就是它），客户端一定认 ✅ |

⇒ 默认走 `legacy`（"平台自己在用什么，我就用什么"这条原则），要试官方新写法把
`at_markup_style` 改成 `new` 即可。

KiraAI 原实现只拼了个纯文本 `@{element.nickname or element.pid}`，所以群里看到的是
**一串 openid 文本**（用户实测："@9CD54739CC9BAA46B93243088802DC72哥ww"）。

> 参考实现：AstrBot 的 qqofficial 发送路径就是
> `plain_text += f'<qqbot-at-user id="{mention_id}" />'`。

插件在 `_text_content` 上做了一层很薄的包装：**只接管 `At` 元素**（换成等价的 `Text`），
其余元素原样交给框架的实现 —— 这样框架以后新增元素类型也不会漏处理。
`pid="all"` 时退化成文本（平台不支持 @全体）。

### 收到的富内容，也都变成能读的东西

官方 bot 的 payload 里有几种形态，KiraAI 的 `_content_elements()` **都不认识**，
到了 LLM 那里要么变成一串乱码、要么直接变成 `[Unsupported message]`：

| 形态 | 原生结果 | 归一化后 |
|---|---|---|
| **语音**（`content_type: "voice"`） | ⚠️ `voice` 不是 mime，框架判不出是音频 → 当成 **File** | ✅ 识别成音频；**有平台自带的 ASR 就直接用文字** |
| **结构化卡片**（`message_type=3` + `ark_data`） | ❌ `[Unsupported message]`，LLM 完全不知道对方发了什么 | ✅ `[卡片: 图文卡片 - 标题 - 描述]` |
| **QQ 表情标记** `<faceType=6, faceId="0", ext="<base64>">` | ❌ 一串 base64 乱码 | ✅ `[表情: 微笑]`（解码 `ext` 里的 JSON） |

**语音那条特别值**：官方语音附件里带 `asr_refer_text` —— 腾讯自己做的免费语音识别。
有它就**直接用文字**（省掉本地 STT 的时间与 token），没有才把 url 换成 `voice_wav_url`
（WAV 更好转写）交给框架。

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
| `tests/test_version_bump.py` | 版本一致性：manifest ⇄ README 标题 ⇄ 最新变更小节 | **5/5** |
| `tests/test_consistency.py` | 一致性 & 静态不变量：schema ⇄ 代码 ⇄ README、裸 await、未用导入、以及几条「踩坑后立的规矩」 | **22/22** |
| `tests/test_bridge.py` | 解析表补丁（含**真实 qq-botpy** 对照）、事件语义、昵称兜底、**@（收发双向，含防冒充）**、**引用（收发+唤醒）**、**富内容归一化（语音/卡片/表情）**、**链类型保留**、REFIDX 提取、**热重载接替**、去重、边界、能力降级、性能与内存、**可逆性** | **173/173** |
| `tests/smoke_v3.py` | **KiraAI 3.0 专项**：世代探测 / 不顶替核心处理器 / 群名 / 引用唤醒补洞（含反向验证）/ markdown·键盘端到端 / 按钮回调 / 工具注入 / 可逆性 | **37/37** |
| `tests/audit_quality.py` | **质量审计**：性能 / 内存有界 / 不阻塞 / 可逆性 / 功能完整性清单 | **48/48** |
| `tests/audit_edge.py` | **边界复审**：核心重建 payload 时键盘是否丢 / 并发串味 / 脏数据 / 异常分类 | **19/19** |
| `tests/audit_promises.py` | **承诺核对**：文档与 PR 说过的行为逐条对照代码，防「说了没做」 | **45/45** |
| `tests/audit_e2e.py` | **端到端链路**：@ 全链路 / 键盘闭环 / 群名 / 群管理工具 / 成员事件 | **31/31** |
| `tests/smoke_real_core.py` | **真实 KiraAI core + 真实 qq-botpy + 真实 `QQOfficialAdapter`** 全链路：原始 payload → 真 `ConnectionState.parsers` → 真 `Client.ws_dispatch` → 真 `KiraMessageEvent`；含 100 条消息压测、「关闭后还原」、**标准 At 渲染与防冒充**、**引用收发**、**发出的 @ 标记**、**语音 ASR / 卡片 / 表情归一化**、跨事件重复观测 | **85/85** |

```bash
# 冒烟需要真实源码路径（找不到会自动跳过）
KIRA_CORE=/path/to/kira_fw BOTPY_PATH=/path/to/botpy python3 tests/smoke_real_core.py
```

### 性能与阻塞（v1.1.5 实测）

| 项目 | 实测 |
|---|---|
| `build_event`——纯文本消息 | **≈ 4.2 µs/条**（≈238,000 条/秒） |
| `build_event`——富消息（语音+表情+@+引用） | **≈ 4.3 µs/条**（≈235,000 条/秒） |
| 200 条富消息串行处理（真实适配器） | **3 ms**（平均 17 µs/条） |
| 5ms ticker 最大调度滞后（= 会不会卡） | **0.7 ms** |
| 去重表 / 昵称通讯录 / 引用索引 | 4096 / 4000 / 1024 条**封顶**（20 万条压测不涨） |

- 消息路径上**没有同步 I/O、没有锁、没有无界循环、没有 await 等待**：
  归一化是纯 CPU（正则 + 小段 base64 解码），富消息和纯文本开销几乎一样
- 昵称通讯录落盘**不在消息路径上**（`asyncio.to_thread`，且只在脏了的时候写，15s 巡检时顺手落盘）
- **唯一的"等待"是 `at_grace_seconds` 的严格模式**（默认 0 = 不等待）；主动消息兜底只在
  被动回复失败时才走一次 HTTP

---

## 更新日志

<details open>
<summary><b>v1.3.0</b> — ★ KiraAI 3.0 兼容（纯增强定位）+ 群名 / markdown / 按钮 / 群管理工具</summary>

### 定位：同一份代码，两种角色

| 核心 | 角色 | 做什么 |
|---|---|---|
| **KiraAI 3.0** | **纯增强插件** | 3.0 已自带全量群消息、真昵称、@ 解析、引用收发、去重、富内容归一化 → 桥接**一律不碰、不另造事件**，只补核心没有的 |
| **KiraAI 2.x** | **补丁型增强** | 核心缺全量群消息解析器、昵称写成 OpenID → 桥接必须补上并接管 |

世代靠**结构探测**（`core_profiles.py`）判断，不看版本号字符串，用户零配置。

### 修掉一个会让 3.0 用户炸掉的问题（重要）

**改造前：把本插件装到 KiraAI 3.0 上，桥接会整体停用**——
3.0 把 IM 相关的一切搬进了 `QQOfficialIMCapability`，`adapter._send_message` /
`_message_chain` 等**全部不存在**，于是 `check_adapter_capabilities` 判定"缺接口"，
桥接打一条 error 后直接 return：主动兜底、引用注入、@ 转 markdown **全部静默失效**。

**更危险的是一颗地雷**：桥接原本对 @ / C2C 事件用了"顶替核心处理器"的策略。
一旦 3.0 给 adapter 补上转发壳，桥接就会顶掉 3.0 原生的 `on_group_at_message_create`，
再用 **2.x 的字段名**造事件 ⇒ **所有 @ 消息静默丢失**。本版按世代严格区分：

* **2.x**：必须接管（核心的昵称实现是 OpenID，顶掉它才修得好）
* **3.0**：必须让位（核心实现已完整，顶掉它等于自杀）

### 新增能力（两家核心都没有）

| 能力 | 说明 |
|---|---|
| **群名** | 后台调 `GET /v2/groups/{openid}/info` 把会话标题换成中文群名。该接口是**白名单（内邀）**，非白名单返回 `11253` → **自动降级为 OpenID（只提示一次，不重试）**，**用户不需要在 QQ 那边做任何设置**；日后加白即自动生效 |
| **markdown** | `<markdown>` 标签 → 富文本。官方 2026-04-23 起，单聊/群聊自定义 markdown **对所有机器人开放，无需申请模板**；被拒（`304036`/`40034127`…）自动退回纯文本并剥掉平台标记 |
| **内联按钮** | `<keyboard>` 标签 → 消息下方挂按钮，带完整校验（行 ≤5 / 每行 ≤5 / `data` ≤100 字符） |
| **按钮点击** | INTERACTION_CREATE → **3 秒内回执**（官方硬要求）→ 转成一条消息给模型 |
| **群管理工具** | `recall_qq_msg` / `set_qq_group_ban` / `get_group_mute_state` / `get_qq_bot_state`，**Route 直发、跨世代可用**，错误码翻译成人话 |
| **成员事件** | 进群/退群/加群申请 → System 消息（需 `extra_intents`，默认关） |
| **3.0 引用唤醒补洞** | 3.0 判"引用是否在叫机器人"用的是**内存里自己发过的消息**，**重启后失效**（实测 `is_mentioned=False`）；桥接改读平台下发的 `author.bot`，重启后仍成立 |

### 架构改造（Phase 0 地基）

* `core_profiles.py`：世代探测 + 落点收敛（adapter vs IMCapability），未知世代只跑工具层；
* **L3 发送增强解耦**：从 `adapter._send_message` 挪到 `client.api.post_*`（两版结构一致），
  3.0 上也能装上；
* **3.0 事件增强**：包 `adapter.publish`（命中缓存才改群名，零 await / 零 I/O）；
* **intent 注入点修正**：包 `botpy.Client.start`（3.0 的 `adapter.start()` 是**阻塞到连接结束**的，
  不能在那里注入）；
* `extra_intents` 默认关 —— 个别环境多订阅会被平台拒并导致连接反复失败，不能拿"能收消息"冒险。

### 测试

| 套件 | 2.x | 3.0 |
|---|---|---|
| `test_bridge.py` | 173 | 173 |
| `smoke_real_core.py` | 88 | 按世代断言 |
| `test_proactive_fallback.py` | 11 | 11 |
| `test_consistency.py` / `test_version_bump.py` | PASSED | PASSED |
| `smoke_v3.py`（新增，3.0 专项） | — | **37** |
| `audit_quality.py`（新增，性能/内存/不阻塞/可逆/功能完整性） | — | **48** |

性能实测：群名查缓存 **0.57 µs/次**、富内容提取 **0.86–1.27 µs/次**、键盘校验 **3.3 µs/次**；
消息路径 3000 次净增长 **0.7 KB**（无泄漏）；群名拉取排队 **0.03 ms** 返回（不阻塞）。

### 全量复审（第二轮）发现并修掉的问题

发布前又做了一轮"逐模块精读 + 承诺核对 + 边界注入 + 端到端链路"复审（新增 3 个套件：
`audit_edge` / `audit_promises` / `audit_e2e`），抓到 **3 个问题**：

1. **承诺未兑现（最严重）**：README 与日志都写着"`extra_intents` 若把连接搞挂，插件会尝试
   自动回退"，但**代码里只写了日志、没有回退逻辑**。现已真正实现：
   包一次 `botpy.gateway.BotWebSocket.__init__` 用弱引用收集网关实例，
   巡检时读 `_can_reconnect`（botpy **只在** `WS_INVALID_SESSION` 时置 False，
   这正是"平台拒绝订阅"的确切信号）或"所有 socket 已关且未重连"，
   命中就自动摘掉 intent 补丁并提示关配置 —— 保住"能收消息"这个基本盘。
2. **作用域保护是死代码**：`api_send` 文档承诺"只对登记过的 api 生效、绝不误伤同进程
   其它 botpy 客户端"，但 `owns()` 从未被调用（`_OWNED` 只写不读）。现已接进补丁入口，
   还原后残留引用也只会原样透传。
3. **死参数/死常量**：`install()` 的 `md_mode / allow_ref / allow_at_md / store_ref`
   四个参数从未被使用（行为其实由插件配置统一决定），以及 `_URL_REJECT` / `_AT_MARKUP` /
   `_self_of` 三处死代码。已全部删除，避免"两套配置各说各话"。

**边界注入验证通过**（19 项）：3.0 核心重建 payload 时键盘**不会丢**、并发发送时
contextvar **不串味**、畸形群名 / 缺字段互动事件 / 超限键盘都**不崩**、
非 markdown 权限类错误**不会被误判成无权限**去静默退纯文本。

**端到端链路验证通过**（31 项）：@ 消息全链路（事件 → 引用 → 真 @ 发出）、
键盘闭环（发键盘 → 点击 → 3 秒回执 → 转消息）、群名（拉取 → 缓存 → 会话标题）、
群管理工具（4 个工具的真实 URL 路径参数绑定与错误翻译）、成员事件。

</details>

<details>
<summary><b>v1.2.0</b> — ★ msg_id 过期不再丢消息（主动消息兜底覆盖 40034005，默认开）</summary>

**根因**（线上实测复现）：官方 bot 的被动回复依赖「最近一次收到消息的 msg_id」，
有效期只有 5 分钟。**跨会话合并路由（session_merger handoff）过来的轮**，触发消息是
合成控制消息，适配器只能拿到 5 分钟前的旧 id → 腾讯返回
**40034005「回复消息msg_id已过期」** → 整条发送失败。
而旧的主动兜底只认 `"needs a received message"` 这一个错误串，且 `proactive_enabled`
**默认关** → 消息无声丢失，LLM 却以为发出去了（线上日志：连续两条
`Failed to send QQ official group message: 回复消息msg_id已过期`，群里什么都没收到）。

**修复**：

- **兜底触发条件扩展**：`needs a received message`（无可用 msg_id）之外，
  新增 `msg_id已过期` / `40034005`（msg_id 已死）——两种被动失效都会改走主动消息接口；
- **命中 40034005 时顺手清掉死 id**（`_group_reply_ids` / `_direct_reply_ids`），
  否则之后每条消息都会先白失败一次再兜底（@ 消息的 markdown 路径还会白失败两次）；
- **`proactive_enabled` 默认改为开**（README 配额说明不变：1000 条/群/天 + 20/qpm
  由官方判定，本地不设限；需群主在群设置里开「机器人主动在群聊内发言」，
  未开时主动发送会失败并打 WARNING——可见，不再静默丢）；
- **主动通道补齐 @ 语义**：带 @ 标记的正文走主动通道时同样改按 markdown 发送
  （纯文本没有 @ 能力），失败退回剥掉标记的纯文本——与被动路径（v1.1.9）同一语义。

影响面：仅 QQ 官方 bot；OneBot（NapCat 等）无被动窗口概念，不涉及。
新增回归测试：`test_bridge.py` +4 用例（过期触发兜底 / 死 id 清除 / 关 proactive 时仍清 id /
主动通道 @ 走 markdown），全量测试见下。

</details>

<details>
<summary><b>v1.1.9</b> — ★ 用 markdown 发 @（揭开「@ 一直显示成文本」的根因）</summary>

**根因**：官方 API 的**纯文本消息（`msg_type=0`）没有 @ 能力** —— 提到标签只在
**markdown 消息（`msg_type=2`）** 里才会被客户端渲染成真的 @。

依据（两处互相印证）：
- 官方《文本交互》页：@ 能力"支持含有文本文字的消息类型，如：文本消息、图文消息、**markdown 消息**"；
- `bunqq-core` 开发文档写得更直白：
  > 含 `<qqbot-at-user id>` 提及标签 → **强制 md（纯文本无 @ 能力）**
  > ⚠️ 纯文本消息无法 @，必须走 `replyMarkdown` 或正文含 md 语法触发自动 md

而 KiraAI 的适配器对文本消息发的正是 `{"msg_type": 0, "content": …}` —— 所以标签永远只会被当文本显示。

**修复**：发出的正文里只要带 @ 标记，**本条自动改按 markdown 发送**（`msg_type=2` + `markdown.content`）。
若该机器人没有 markdown 消息权限导致发送失败 ⇒ **自动退回纯文本并剥掉标记**
（宁可少一个 @，也不把 `<qqbot-at-user id="…" />` 原样发到群里），并打一条 WARNING 说明原因。

</details>

<details>
<summary><b>v1.1.8</b> — 旧补丁层会被顶掉（@ 仍不生效的真凶）+ 引用索引适配器级共享</summary>

用户在 v1.1.7 上反馈：日志明确写了「已记录第 1 个 REFIDX」，但真要引用时却报
「没找到对应的 REFIDX（已知 5 条）」。

### ★ 首要修复：热重载残留的**旧补丁层**会被顶掉

v1.1.7 及更早的版本里，如果 adapter 上**已经有一层补丁**（多为热重载残留），代码会**直接跳过**：

```python
original = getattr(adapter, "_text_content", None)
if not callable(original) or getattr(original, "_kira_bridge_at", False):
    return          # ← 于是旧实例的包装一直在跑
```

后果：新版本的能力（正文 @ 归一化）**根本没装上**，跑的还是旧包装 ——
表现就是「At 仍然用旧形态 + 正文里的标记原样发出去」（用户实测正是如此）。

⇒ 现在检测到旧层会 **warning + 接管**：从 `_qqbot_bridge_text_orig` 找到**真正的原始实现**，
用新实例的包装替换掉旧层。api 侧（`message_reference`）同理。

> **更新插件后请彻底重启 KiraAI**（只重载插件不一定能清掉旧实例的补丁层）。

### 引用索引的两个根因，一起堵掉：

1. **各存一份**：引用索引原来挂在**插件实例**上；热重载/多次加载时，收消息的实例和发消息的实例
   可能不是同一个 ⇒ 记录到了 A 的表里，B 去查自然查不到。
   ⇒ 现在挂在**适配器对象**上共享（`ref_store_for(adapter)`）。
2. **id 口径**：模型有时会回**原始 message id**，而框架渲染的是 `qqo-xxxx` 短 id。
   ⇒ 现在**两个键都记**（展示态 id + 原始 id），查哪个都能命中。

**诊断加强**（下次一眼定位）
- 记录时打前 3 条：`引用索引 +1：键=(qq:gm:xxx, qqo-xxxx) ← REFIDX_yyy==`
- 查找失败时把**已知的 id 列出来**：`没找到对应的 REFIDX（已知 5 条：qqo-abcd…, qqo-efgh…）`

**缓存友好性（同时加固）**
- 新增断言：**50 条无关消息之后再重放同一条 → 渲染与首次逐字节一致**
  （证明注入内容是「消息 + 已学身份」的纯函数，不受后续状态影响）
- 审计确认：v1.1.7 / v1.1.8 的改动**全在发送侧**，没碰任何 LLM 看得到的内容

</details>

<details>
<summary><b>v1.1.7</b> — 正文里模型自己写的 @ 标记也会被归一化</summary>

**@ 为什么还是一串文本？**
上一版只改了**At 元素**的输出形态，但群里那串 `<qqbot-at-user … />` 其实还有**第二个来源：模型在模仿** ——
历史里已经出现过这个标记，模型就会在正文里照样写一份。它是普通字符串、不是 At 元素，于是被原样发了出去。

⇒ 现在**发出的正文**里任何 `<qqbot-at-user id="…" />` / `<@…>` 都会按 `at_markup_style` 归一化，
首次命中时打一条 INFO（方便确认来源到底是谁）。

**其它**
- REFIDX 解析更宽容（URL 编码 / 引号 / 多余空格 / 字典形式都能取到）
- 启动日志新增 `@标记形态=`，一眼看出当前生效的配置

</details>

<details>
<summary><b>v1.1.6</b> — @ 标记形态可配 + 引用语音可读 + 引用诊断</summary>

**@ 标记形态**
- 官方文档推荐 `<qqbot-at-user id="…" />`，但**实测在部分环境会被当纯文本原样显示**；
  默认改用 **legacy 形态 `<@openid>`** —— 那是**平台自己下发给我们**用的写法，客户端一定认
- 新增 `at_markup_style`（`legacy` / `new`）可随时切换

**引用里的富内容**
- 之前只归一化顶层 body，**引用元素（`msg_elements`）里的语音/卡片/表情没被处理** ⇒
  「引用一条语音」时 LLM 读不出内容、容易答非所问。现在引用元素同样归一化

**可观测（引用回复诊断）**
- 首次拿到 REFIDX 会打一条 INFO；连续 3 条消息都没有 `message_scene.ext.msg_idx` 会打 WARNING
  （带 body 顶层键与 `message_scene` 样本）
- 机器人想引用却找不到 REFIDX 时提示一次（本条按普通回复发出）

</details>

<details>
<summary><b>v1.1.5</b> — 紧急修复：消息链类型丢失</summary>

**问题（v1.1.1 引入的回归）**
- 拆完 @ 之后把消息链换成了普通 `list`，丢掉了 KiraAI 的 `MessageChain` 类型
- S 版 / 其他聊天插件的 `_process_media()` 会直接访问 `chain.message_list`
  ⇒ `AttributeError: 'list' object has no attribute 'message_list'`
  ⇒ handler 抛异常 ⇒ **聊天插件对每条消息都失效**（用户实测）

**修复**
- 拆分链之后**就地还原链类型**（`chain.message_list = ...`，对象身份保持不变）
- 补两条回归断言：普通消息、以及拆过 @ 的消息，链都必须是 `MessageChain`

</details>

<details>
<summary><b>v1.1.4</b> — 富内容归一化（语音 / 卡片 / 表情）</summary>

- **语音**：官方 `content_type` 写的是 `voice`（不是 mime），框架会误判成 File；
  现在归一化成音频，并且**优先用平台自带的免费 ASR**（`asr_refer_text`）——
  有它就直接拿文字，不再跑本地 STT
- **结构化卡片**（`message_type=3` + `ark_data`）：原来 LLM 只看到 `[Unsupported message]`，
  现在渲染成 `[卡片: 名称 - 标题 - 描述]`
- **QQ 表情标记** `<faceType=.., faceId=.., ext="base64">`：解码 `ext` 里的 JSON，
  渲染成 `[表情: 微笑]`（解码失败退化成 `[表情]`，不会崩）

</details>

<details>
<summary><b>v1.1.3</b> — 机器人 @ 人也是真 @</summary>

- 平台发送侧要求 `<qqbot-at-user id="openid" />` 才会渲染成真正的提及；
  KiraAI 原实现只拼了纯文本 `@昵称`，群里显示的是一串 openid 文本（用户实测）
- 插件只接管 `At` 元素（换成等价 `Text`），其余元素原样交给框架实现
- 收到的事件里两种 @ 形态（`<@openid>` 与 `<qqbot-at-user id="..." />`）**都能解析**
- `pid="all"` 退化成文本（平台不支持 @全体）

</details>

<details>
<summary><b>v1.1.2</b> — 引用回复（收发双向）</summary>

**机器人也能「引用回复」**
- 官方发送接口支持 `message_reference`（文档："引用回复。填写后以引用形式展示，关联上下文"），
  KiraAI 只是没传这个字段 —— 补上即可，**不需要任何额外连接**
- 用官方要求的 `REFIDX`（不是消息 id）：别人发的取事件 `message_scene.ext.msg_idx`，
  机器人自己发的取发送响应 `ext_info.ref_idx`
- 只在机器人**主动引用**时才带（否则每条都会变成引用上一条）

**被引用回复 = 被提及（对齐 OneBot）**
- 有人引用机器人的消息时唤醒它 —— 与 KiraAI 的 QQ(OneBot) 适配器一致
  （那边遇到 `reply` 段会反查被引用消息的作者是不是自己）
- QQ 官方这边不用额外请求：被引用消息的作者就在 `msg_elements[0].author` 里
- 引用**别人**的消息不算唤醒；引用内容里的 @ 也不算（历史内容）

**修复：热重载后仍跑旧代码**
- 插件重载时，旧实例留在 client 上的 handler 会被新实例**接替**（之前会一直用旧的，
  导致"代码更新了但行为没变"）

**可观测**
- 启动日志带上版本号与各项开关状态，一眼看出跑的是哪一版

</details>

<details>
<summary><b>v1.1.1</b> — 认得出「机器人自己被 @」</summary>

**@ 富文本解析（KiraAI 标准格式）**
- 官方 bot 群消息里的 @ 是富文本标记（形如 `<@32位hex>`），原生实现不解析，
  LLM 只看到一串 hex、连「有人在叫我」都判断不出来
- 现在解析成 **KiraAI 标准 `At` 元素**：`[At 香里（你）(0A0B9F...)] 妹`
- **pid 永远保留**：昵称用户随时能改，只显示昵称会被改名冒充；
  KiraAI 原版约定就是「名字给人读，pid 做身份」（OneBot 路径甚至只给 `[At QQ号]`）
- 自己那个 `At` 的名字带「（你）」后缀，同名冒充也分得清
- 内容里出现「自己的 @」时**强制** `is_mentioned=True` —— 不依赖平台是否给 `is_you`
- **@ 他人同样拆成标准 `At`**（各带各的 pid），只 @ 别人不算唤醒
- **引用消息**（`message_type=103`）解析成标准 `Reply` + 被引用原话；
  引用内容里的 @ 也拆 `At`，但**不算"现在在叫我"**（历史消息不触发误唤醒）
- **「被引用回复」= 被提及**：有人引用机器人的消息时唤醒它（`reply_to_self_wakes`，
  对齐 KiraAI 的 OneBot 适配器 —— 那边遇到 `reply` 段会反查作者是不是自己）
- **机器人也能引用回复**（`quote_reply`）：自动带上官方要求的 `REFIDX`
  （`message_scene.ext.msg_idx` / 发送响应 `ext_info.ref_idx`），消息以引用形式展示

**认出「自己」**
- 三级识别：`mentions[].is_you` → `bot=true` 且昵称与机器人名字一致 → 兜底反推
  （内容里有、`mentions` 里查不到的 @ ⇒ 就是机器人自己）
- 新增 `self_openid` 配置：留空＝全自动；自动识别猜错时可钉死
- 首次遇到 @ 富文本打样本日志（原文 / 解析后 / mentions / 认出的 OpenID 及来源）

**新增配置**
- `resolve_at_markup`（默认开）、`learn_self_openid`（默认开）、`self_openid`（默认空）

</details>

<details>
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

**自我识别（本版新增）**
- 新增 @ 富文本解析：`<@openid>` → `@昵称`，机器人自己 → `@<机器人名字>`
- 自动学出机器人自己的 OpenID（`is_you` → `bot+昵称` → 兜底反推），并可手动钉死
- 内容里出现"自己的 @"时强制 `is_mentioned=True`，不再依赖平台是否给 `is_you`

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
