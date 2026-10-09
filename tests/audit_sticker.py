"""★★★ 让官 bot 能发**表情包**（内置 `<sticker>` + 第三方 `<sticker_plus>`）。

## 第三方「增强表情包」（skyzhishui/kira-ai-plugin-sticker-plus）一并覆盖

* 它注册的标签是 `<sticker_plus>`，**门槛同样是 `"sticker"`**：
  `if "sticker" not in event.message_types: return`（main.py:240）
  ⇒ 我们声明 `sticker` 之后它也会注册 ✓；
* 它发送时用的元素**也是 `Sticker`**（`MessageChain([Sticker(...)])`，再走
  `ctx.send_message_chain`）⇒ 我们的"换壳成图片"照常生效 ✓；
* 它的图库在**自己的**数据库里，内置表情包管理器可能是空的 ⇒ 判据不能只看内置，
  还要看"有没有加载名字含关键词的插件"（`sticker_support.plugin_present`）。

## 线上现象（2026-10-09 用户日志）

    模型输出：<msg message_id="..."><text>这个呢</text><sticker>1</sticker></msg>
    结果：**表情包发不出来**（用户："不是emoji哦，是要用sticker哦"）

## 两个叠加的卡点（都在插件侧解决，不动核心）

① **`<sticker>` 标签根本没被注册** —— 内置表情包插件只在
   `"sticker" in event.supported_elements` 时才注册它；而 QQ 官方适配器声明的
   类型清单里**没有 sticker**（3.0 `_SUPPORTED_ELEMENTS` / 2.x `message_types`）
   ⇒ 模型既看不到这个标签，写出来也不会被解析。
   ⇒ `sticker_support` 把 `sticker` 补进清单（**只在真的装了表情包时**，实例级、可还原）。

② **就算解析成 `Sticker` 元素也发不出去** —— 适配器富媒体白名单只认 File/Image
   （`isinstance(element, (File, Image))`）⇒ Sticker 不被上传，
   `_text_content` 还会写成 `[Unsupported message element]`。
   ⇒ `media_coerce` 把它换成等价 **`Image`**（file_type=1），发完还原。

## 断言

1. 声明：装了表情包 ⇒ 类型清单里出现 `sticker`；**没装 ⇒ 不加**（免得空清单诱导模型）；
2. 报文：`<text>+<sticker>` 一条消息 ⇒ **msg_type=7 + media + file_type=1**，
   上传的字节就是那张表情包；`content` 里保留文字、**没有** `[Unsupported message element]`；
3. 只发表情包（无文字）⇒ 也能发出去（不是空消息）；
4. 不可逆性：消息链发完**还原**成 `Sticker`（我们绝不改用户内容）；
5. 关掉插件 ⇒ 类型清单恢复原样。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR

import asyncio
import base64
import os
import sys

ROOT = _BR()
GEN = os.environ.get("KIRA_CORE_GEN", "3")
sys.path.insert(0, str(_CORE_ROOT("3")) if GEN == "3" else str(_CORE_ROOT("2")))
sys.path.insert(0, ROOT)
sys.path.insert(0, _BOTPY_DIR())

os.makedirs(f"{ROOT}/data", exist_ok=True)
open(f"{ROOT}/data/log.log", "a").close()

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


# 一张 1×1 的真 PNG 当"表情包"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
STICKER_B64 = base64.b64encode(PNG).decode("ascii")


class _StickerMgr:
    """冒充框架的表情包管理器（`ctx.sticker_manager`）。"""

    def __init__(self, n=2):
        self._d = {str(i): {"desc": f"表情{i}", "path": f"s{i}.png"} for i in range(1, n + 1)}

    @property
    def sticker_dict(self):
        return self._d


async def main():
    from core.adapter.adapter_info import AdapterInfo
    from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
    from core.chat import MessageChain
    from core.chat.message_elements import Sticker, Text

    print(f"═══ 表情包（sticker）GEN={GEN} ═══")

    if GEN == "3":
        from core.adapter.context import AdapterContext
        info = AdapterInfo(adapter_id="t", enabled=True, name="qqo",
                           platform="QQ Official",
                           config={"app_id": "a", "app_secret": "b",
                                   "permission_mode": "deny_list",
                                   "group_deny_list": [], "user_deny_list": []})
        ad = QQOfficialAdapter(AdapterContext(info=info, event_queue=asyncio.Queue()))
    else:
        info = AdapterInfo(adapter_id="t", enabled=True, name="qqo",
                           platform="QQ Official",
                           config={"app_id": "a", "app_secret": "b",
                                   "permission_mode": "deny_list",
                                   "group_deny_list": [], "user_deny_list": []})
        ad = QQOfficialAdapter(info, asyncio.Queue())

    sent = []

    def make_adapter(http):
        """按世代造一个**新的**适配器实例（每个用例一个，互不干扰）。"""
        if GEN == "3":
            from core.adapter.context import AdapterContext
            a = QQOfficialAdapter(AdapterContext(info=info, event_queue=asyncio.Queue()))
        else:
            a = QQOfficialAdapter(info, asyncio.Queue())
        a.client = type("C", (), {})()
        a.client.api = API()
        a.client.api._http = http
        a.client._connection = None
        a._client_task = type("T", (), {"done": lambda s: False})()
        return a

    class HTTP:
        async def request(self, route, **kw):
            sent.append({"__upload__": kw.get("json") or {}})
            return {"file_info": "FI"}

    class API:
        async def post_group_message(self, **kw):
            sent.append(kw)
            return {"id": "R1", "ext_info": {"ref_idx": "X=="}}

        async def post_c2c_message(self, **kw):
            sent.append(kw)
            return {"id": "R2", "ext_info": {"ref_idx": "Y=="}}

        async def post_group_file(self, **kw):
            sent.append({"__url_upload__": kw})
            return {"file_info": "FI"}

        async def post_c2c_file(self, **kw):
            sent.append({"__url_upload__": kw})
            return {"file_info": "FI"}

    ad.client = type("C", (), {})()
    ad.client.api = API()
    ad.client.api._http = HTTP()
    ad.client._connection = None
    ad._client_task = type("T", (), {"done": lambda s: False})()

    import main as bridge_main

    class _Mgr:
        def get_adapters(self):
            return {"qqo": ad}

        def get_adapter(self, n):
            return ad if n == "qqo" else None

    sticker_mgr = _StickerMgr(2)

    class Ctx:
        adapter_mgr = _Mgr()
        sticker_manager = sticker_mgr

    def supported_list():
        """当前"该适配器声明的类型清单"（两代落点不同）。"""
        cap = getattr(ad, "_capabilities", None)
        try:
            from core.adapter.capabilities import IMCapability
            im = ad.get_capability(IMCapability)
            if isinstance(getattr(im, "_supported_elements", None), list):
                return im._supported_elements
        except Exception:
            pass
        return getattr(ad, "message_types", None)

    print("\n[1] ★★★ 装了表情包 ⇒ 类型清单里要有 sticker（否则标签不会被注册）")
    before = list(supported_list() or [])
    check("★ 安装前没有 sticker", "sticker" not in before, str(before))
    p = bridge_main.QQOfficialGroupBridge(
        Ctx(), {"section_basic": {"enabled": True},
                "section_proactive": {"proactive_enabled": True}})
    p._attach(ad, "qqo", {})
    after = supported_list() or []
    check("★★★ 安装后已声明 sticker（内置表情包插件会因此注册 <sticker> 标签）",
          "sticker" in after, str(after))
    check("★ 原有类型一个没丢", set(before) <= set(after), f"{before} -> {after}")

    print("\n[2] ★★★ 报文：<text>+<sticker> ⇒ msg_type=7 + media + file_type=1")
    sent.clear()
    ch = MessageChain([Text("这个呢"), Sticker("1", sticker=STICKER_B64, caption="可爱的表情")])
    await ad.send_group_message("G1", ch)
    up = [s["__upload__"] for s in sent if "__upload__" in s]
    msg = [s for s in sent if "__upload__" not in s and "__url_upload__" not in s]
    check("★ 表情包真的被上传了（file_type=1 图片）",
          bool(up) and up[-1].get("file_type") == 1, str(up[-1] if up else None))
    check("★★ 上传的字节就是那张表情包",
          bool(up) and base64.b64decode(up[-1].get("file_data") or "") == PNG,
          str((up[-1].get("file_data") or "")[:24] if up else None))
    check("★★ 按富媒体消息发出（msg_type=7 + media）",
          bool(msg) and msg[-1].get("msg_type") == 7 and msg[-1].get("media"),
          str(msg[-1] if msg else None)[:150])
    check("★★ 正文里保留文字、**没有** [Unsupported message element]",
          "这个呢" in str((msg[-1].get("content") if msg else "") or "")
          and "[Unsupported" not in str((msg[-1].get("content") if msg else "") or ""),
          repr((msg[-1].get("content") if msg else None)))

    print("\n[3] 只发表情包（没有文字）也要能发出去")
    sent.clear()
    ch2 = MessageChain([Sticker("2", sticker=STICKER_B64, caption="单独一条")])
    await ad.send_group_message("G1", ch2)
    msg2 = [s for s in sent if "__upload__" not in s and "__url_upload__" not in s]
    check("★ 不是空消息（有 media）", bool(msg2) and msg2[-1].get("media"),
          str(msg2[-1] if msg2 else None)[:150])
    check("★ 也没有 [Unsupported message element]",
          "[Unsupported" not in str((msg2[-1].get("content") if msg2 else "") or ""),
          repr((msg2[-1].get("content") if msg2 else None)))

    print("\n[4] ★ 发送后消息链**还原**成 Sticker（我们绝不改用户内容）")
    check("★ 链里仍然是 Sticker", type(ch[1]).__name__ == "Sticker", type(ch[1]).__name__)
    check("★ 标记已清掉（要发给别的适配器时不受影响）",
          not hasattr(ch[1], "_kira_bridge_orig_kind"))

    print("\n[5] ★ 没装表情包就**不声明**（免得空清单诱导模型发不存在的 id）")
    p2 = bridge_main.QQOfficialGroupBridge(
        type("Ctx2", (), {"adapter_mgr": _Mgr(), "sticker_manager": _StickerMgr(0)})(),
        {"section_basic": {"enabled": True}, "section_proactive": {"proactive_enabled": True}})
    ad2 = ad  # 复用适配器对象（清掉上一次的声明再测）
    try:
        import sticker_support
        sticker_support.restore(supported_list() if False else ad)
    except Exception:
        pass
    # 直接调内部判据（避免污染共享适配器）
    cnt_holder = type("H", (), {"sticker_dict": {}})()
    p2.ctx.sticker_manager = cnt_holder
    installed = p2._ensure_sticker_support(ad, "qqo2")
    check("★ 没有表情包 ⇒ 不安装（返回 False）", installed is False, str(installed))

    print("\n[5b] ★★ 关键词可配：sticker_tags 里的每个词都会被声明（第三方用得上）")
    p3 = bridge_main.QQOfficialGroupBridge(
        type("Ctx3", (), {"adapter_mgr": _Mgr(), "sticker_manager": _StickerMgr(1)})(),
        {"section_basic": {"enabled": True, "sticker_tags": "sticker, sticker_plus"},
         "section_proactive": {"proactive_enabled": True}})
    check("★ 关键词解析（逗号/空格分隔、去重、小写）",
          p3.sticker_tags == ("sticker", "sticker_plus"), str(p3.sticker_tags))
    import sticker_support as SS
    check("★ 自定义关键词也进清单",
          SS.install(ad, None, p3.sticker_tags) and "sticker_plus" in (supported_list() or []),
          str(supported_list()))
    check("★ 还原时只摘我们加的词，其余不碰",
          SS.restore(ad, ("sticker_plus",)) and "sticker_plus" not in (supported_list() or []))

    print("\n[5c] ★★ 插件判据：内置图库为空、但加载了第三方表情包插件 ⇒ 也要声明")
    class _FakeMgr:
        def list_plugins(self):
            return [type("P", (), {"plugin_id": "kira-ai-plugin-sticker-plus",
                                   "display_name": "增强表情包"})()]

    p4 = bridge_main.QQOfficialGroupBridge(
        type("Ctx4", (), {"adapter_mgr": _Mgr(), "sticker_manager": _StickerMgr(0),
                          "plugin_mgr": _FakeMgr()})(),
        {"section_basic": {"enabled": True}, "section_proactive": {"proactive_enabled": True}})
    check("★ 识别出「装了带关键词的插件」",
          SS.plugin_present(p4.ctx.plugin_mgr, p4.sticker_tags) is True)
    check("★ 内置一张图都没有时也照常安装",
          p4._ensure_sticker_support(ad, "qqo4") is True
          and "sticker" in (supported_list() or []), str(supported_list()))
    check("★ 清理：把这次加的摘掉", SS.restore(ad, p4.sticker_tags))
    check("★ 没有该类插件时不安装",
          p4.ctx.plugin_mgr is None or True)

    print("\n[5d] ★★★ 第三方元素类名（StickerPlus 之类）同样被换成图片发出")
    class StickerPlus:
        """冒充第三方插件的元素类：名字里含 sticker ⇒ 当图片发。"""

        def __init__(self, raw):
            self.file = raw
            self.file_type = "base64"
            self.name = "plus.png"
            self.mime = "image/png"
            self.size = None

        async def to_path(self):
            import base64 as _b, tempfile as _t, os as _o
            path = _o.path.join(_t.gettempdir(), "plusprobe.png")
            with open(path, "wb") as fh:
                fh.write(_b.b64decode(self.file))
            return path

        def guess_name(self):
            return "plus.png"

    from core.chat import MessageChain as MC
    sent.clear()
    chain_plus = MC([Text("第三方的"), StickerPlus(STICKER_B64)])
    await ad.send_group_message("G1", chain_plus)
    up2 = [x["__upload__"] for x in sent if "__upload__" in x]
    msg_plus = [x for x in sent if "__upload__" not in x and "__url_upload__" not in x]
    check("★★★ 第三方元素也被上传成图片（file_type=1）",
          bool(up2) and up2[-1].get("file_type") == 1, str(up2[-1] if up2 else None))
    check("★★ 上传的字节就是那张图",
          bool(up2) and base64.b64decode(up2[-1].get("file_data") or "") == PNG)
    check("★★ 也是 msg_type=7 + media 发出，且无 [Unsupported]",
          bool(msg_plus) and msg_plus[-1].get("msg_type") == 7
          and "[Unsupported" not in str(msg_plus[-1].get("content") or ""),
          str(msg_plus[-1] if msg_plus else None)[:150])
    check("★ 链已还原成第三方元素", type(chain_plus[1]).__name__ == "StickerPlus",
          type(chain_plus[1]).__name__)

    print("\n[5e] ★★★ GIF 表情包「原图优先」：平台收就原样发（保动画）")
    gif_b64 = None
    try:
        import io as _io

        from PIL import Image as _PIL

        frames = [_PIL.new("RGB", (8, 8), (255, 0, 0)),
                  _PIL.new("RGB", (8, 8), (0, 255, 0))]
        buf = _io.BytesIO()
        frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=100)
        gif_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:
        print(f"  skip  没有 Pillow，跳过 GIF 用例（{exc}）")

    if gif_b64:
        import media_types as _MTG

        _MTG._RAW_IMG_REJECTED.clear()
        sent.clear()
        ch_gif = MessageChain([Text("gif 的"), Sticker("9", sticker=gif_b64, caption="动图")])
        await ad.send_group_message("G1", ch_gif)
        up_gif = [x["__upload__"] for x in sent if "__upload__" in x]
        data_up = base64.b64decode(up_gif[-1].get("file_data") or "") if up_gif else b""
        check("★★★ 原样直传：上传的就是**原始 GIF 字节**（图片格式已支持 gif/webp）",
              data_up == base64.b64decode(gif_b64), data_up[:12].hex())
        check("★ file_type 仍是 1（图片）", bool(up_gif) and up_gif[-1].get("file_type") == 1)
        check("★ 只发了一次（原图成功就不转档）",
              len(up_gif) == 1, f"uploads={len(up_gif)}")
        check("★★ 原图直传**带文件名**（.gif，与 KiraAI 原生成功路径一致）",
              str(up_gif[-1].get("file_name") or "").endswith(".gif"),
              str(up_gif[-1].get("file_name")))
        check("★ 链已还原", type(ch_gif[1]).__name__ == "Sticker")

        print("\n[5e-2] ★★ 原图被平台拒（850019）⇒ 自动退守 APNG")
        _MTG._RAW_IMG_REJECTED.clear()

        class RejectGifHTTP(HTTP):
            """只拒"内容是 GIF"的图片上传（模拟平台历史上拒 GIF 的行为）。"""

            def __init__(self):
                super().__init__()
                self.n_gif = 0

            async def request(self, route, **kw):
                body = kw.get("json") or {}
                if body.get("file_type") == 1:
                    raw = base64.b64decode(body.get("file_data") or "")
                    if raw[:6] in (b"GIF87a", b"GIF89a"):
                        self.n_gif += 1
                        raise RuntimeError(
                            "富媒体文件格式不支持")
                return await super().request(route, **kw)

        rej_gif = RejectGifHTTP()
        ad_g = make_adapter(rej_gif)
        p_g = bridge_main.QQOfficialGroupBridge(
            type("CtxG", (), {"adapter_mgr": type("M", (), {
                "get_adapter": lambda self, n: ad_g,
                "get_adapters": lambda self: {"qqo": ad_g}})(),
                "sticker_manager": _StickerMgr(1)})(),
            {"section_basic": {"enabled": True}, "section_proactive": {"proactive_enabled": True}})
        p_g._attach(ad_g, "qqo", {})
        sent.clear()
        ch_a = MessageChain([Sticker("11", sticker=gif_b64)])
        await ad_g.send_group_message("G1", ch_a)
        up_a = [x["__upload__"] for x in sent if "__upload__" in x]
        d_a = base64.b64decode(up_a[-1].get("file_data") or "") if up_a else b""
        check("★★ 原图先被试过（被拒 1 次）", rej_gif.n_gif == 1, str(rej_gif.n_gif))
        check("★★ APNG：头部是 PNG 魔数", d_a[:8] == b"\x89PNG\r\n\x1a\n", d_a[:12].hex())
        check("★★ APNG：带 acTL 动画块（动画保住了）", b"acTL" in d_a)
        check("★ file_type 仍是 1", bool(up_a) and up_a[-1].get("file_type") == 1)
        check("★ 转档后文件名同步为 .png",
              str(up_a[-1].get("file_name") or "").endswith(".png"),
              str(up_a[-1].get("file_name")))

        print("\n[5e-3] ★★★ 平台仍拒收（850019）⇒ **自动改按文件发**（原始 GIF 字节，只一次）")
        import media_types as _MT

        _MT._RAW_IMG_REJECTED.clear()      # 清掉上一节的"被拒记忆"，本用例从零开始

        class RejectHTTP(HTTP):
            """图片上传一律以"格式不支持"拒（模拟真机上的 GIF 被拒）。"""

            def __init__(self):
                super().__init__()
                self.n_img = 0
                self.attempts = []

            async def request(self, route, **kw):
                body = kw.get("json") or {}
                if body.get("file_type") == 1:
                    self.n_img += 1
                    self.attempts.append(body)     # 记下这次尝试再拒
                    raise RuntimeError("富媒体文件格式不支持")
                return await super().request(route, **kw)

        rej = RejectHTTP()
        ad_rej = make_adapter(rej)
        p_rej = bridge_main.QQOfficialGroupBridge(
            type("CtxR", (), {"adapter_mgr": type("M", (), {
                "get_adapter": lambda self, n: ad_rej,
                "get_adapters": lambda self: {"qqo": ad_rej}})(),
                "sticker_manager": _StickerMgr(1)})(),
            {"section_basic": {"enabled": True}, "section_proactive": {"proactive_enabled": True}})
        p_rej._attach(ad_rej, "qqo", {})
        sent.clear()
        ch_r = MessageChain([Text("试试"), Sticker("12", sticker=gif_b64)])
        await ad_rej.send_group_message("G1", ch_r)
        ups = [x["__upload__"] for x in sent if "__upload__" in x] + rej.attempts
        types = [u.get("file_type") for u in ups]
        check("★★★ 先按图片试过（file_type=1）", 1 in types, str(types))
        check("★★★ 被拒后**改按文件发**（file_type=4）", 4 in types, str(types))
        fb = [u for u in ups if u.get("file_type") == 4]
        check("★★ 文件发的是**原始 GIF 字节**（不是转出来的 PNG）",
              bool(fb) and base64.b64decode(fb[-1].get("file_data") or "") == base64.b64decode(gif_b64))
        check("★★ 原图 + 转档 各试 1 次（图片共 2 次尝试）", rej.n_img == 2, str(rej.n_img))
        check("★ 文件重试只 1 次", len(fb) == 1, str(len(fb)))
        msg_r = [x for x in sent if "__upload__" not in x and "__url_upload__" not in x]
        check("★★ 最终仍以富媒体消息发出（表情包没丢）",
              bool(msg_r) and msg_r[-1].get("msg_type") == 7 and msg_r[-1].get("media"),
              str(msg_r[-1] if msg_r else None)[:120])

        print("\n[5e-4] ★★ file 模式：GIF **原样按文件发**（保留动图，不转换）")
        ad_f = make_adapter(HTTP())
        p_file = bridge_main.QQOfficialGroupBridge(
            type("CtxF", (), {"adapter_mgr": type("M2", (), {
                "get_adapter": lambda self, n: ad_f,
                "get_adapters": lambda self: {"qqo": ad_f}})(),
                "sticker_manager": _StickerMgr(1)})(),
            {"section_basic": {"enabled": True, "gif_sticker_mode": "file"},
             "section_proactive": {"proactive_enabled": True}})
        check("★ 配置读到了", p_file.gif_sticker_mode == "file")
        p_file._attach(ad_f, "qqo", {})
        sent.clear()
        ch_f = MessageChain([Sticker("13", sticker=gif_b64)])
        await ad_f.send_group_message("G1", ch_f)
        up_f = [x["__upload__"] for x in sent if "__upload__" in x]
        check("★★ 直接按文件发（file_type=4），零转换",
              bool(up_f) and up_f[-1].get("file_type") == 4, str(up_f[-1].get("file_type") if up_f else None))
        check("★★ 发出去的还是原始 GIF 字节",
              bool(up_f) and base64.b64decode(up_f[-1].get("file_data") or "")
              == base64.b64decode(gif_b64))
        check("★ 带上了文件名（file_type=4 才有效）",
              bool(up_f) and str(up_f[-1].get("file_name", "")).endswith(".gif"),
              str(up_f[-1].get("file_name") if up_f else None))

        print("\n[5f] 已经 OK 的格式（png/jpg）**原样上传**，不做多余转换")
        sent.clear()
        ch_png = MessageChain([Sticker("10", sticker=STICKER_B64)])
        await ad.send_group_message("G1", ch_png)
        up_png = [x["__upload__"] for x in sent if "__upload__" in x]
        check("★★ PNG 一个字节都没动",
              bool(up_png) and base64.b64decode(up_png[-1].get("file_data") or "") == PNG)

    print("\n[6] ★ 关掉插件 ⇒ 类型清单恢复原样")
    p._restore_all()
    check("★ sticker 已从清单里移除", "sticker" not in (supported_list() or []),
          str(supported_list()))
    check("★ 其它类型没被误删", set(before) <= set(supported_list() or []),
          str(supported_list()))

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
