
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR
import os
import asyncio, os, sys
ROOT=_BR()
GEN=os.environ.get("GEN","3")
sys.path.insert(0, str(_CORE_ROOT(GEN)))
sys.path.insert(0, ROOT); sys.path.insert(0,_BOTPY_DIR())
os.makedirs(ROOT+"/data",exist_ok=True); open(ROOT+"/data/log.log","a").close()
import media_types as MT
P=F=0
def ck(n,c,e=""):
    global P,F
    if c: P+=1; print("  ok   "+n)
    else: F+=1; print(f"  FAIL {n}  {e}")

print(f"═══ media_types GEN={GEN} ═══")
from core.chat.message_elements import Image, Video, Record, File

for cls, want, label in [(Image,None,"Image"),(Video,2,"Video"),(Record,3,"Record"),(File,None,"File")]:
    o=cls.__new__(cls)
    got=MT.classify(o)
    ck(f"classify({label}) = {want}", got==want, f"拿到 {got}")

# 安装到真实 holder
from core.adapter.adapter_info import AdapterInfo
from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
info=AdapterInfo(adapter_id="t",enabled=True,name="qqo",platform="QQ Official",
  config={"app_id":"a","app_secret":"b","permission_mode":"deny_list","group_deny_list":[],"user_deny_list":[]})
if GEN=="3":
    from core.adapter.context import AdapterContext
    ad=QQOfficialAdapter(AdapterContext(info=info,event_queue=asyncio.Queue()))
    holder=ad.im
else:
    ad=QQOfficialAdapter(info, asyncio.Queue()); holder=ad
ad.client=type("C",(),{})(); ad.client.api=type("A",(),{})()
before=holder._upload_file
before_f=getattr(before,"__func__",before)
ok=MT.install(holder, ad.client)
ck("install 返回 True", ok)
now_f=getattr(holder._upload_file,"__func__",holder._upload_file)
ck("★ _upload_file 已被替换", now_f is not before_f)
ck("幂等（二次 install 不重复包）", MT.install(holder, ad.client))
ok_re=MT.restore(holder)
back_f=getattr(holder._upload_file,"__func__",holder._upload_file)
ck("可还原", ok_re and back_f is before_f, f"restore={ok_re}")
print(f"\n结果：{P} passed, {F} failed")
sys.exit(1 if F else 0)
