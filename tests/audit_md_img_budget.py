"""验证总预算：图片转存太慢时必须放弃、让消息先发出去。"""
import asyncio, os, sys, time
ROOT="/var/minis/workspace/qqbot_bridge_review"
sys.path.insert(0, ROOT+"/kira-v3"); sys.path.insert(0, ROOT+"/bridge")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.makedirs(ROOT+"/data",exist_ok=True); open(ROOT+"/data/log.log","a").close()
import md_media as M

class L:
    def warning(self,*a): print("    WARN:", (a[0]%tuple(a[1:])) if len(a)>1 else a[0])
    def info(self,*a): pass
    def debug(self,*a): pass

async def main():
    # 把预算调小便于测试
    M._IMAGE_BUDGET_SECONDS = 1.0
    async def hang(client, tid, isg, url, logger=None, timeout=30.0):
        await asyncio.sleep(30)     # 模拟很慢的转存
        return url + "?conv=1"
    M.upload_remote_to_public_url = hang
    M.clear_caches()

    md = "![图0](https://ex.com/0.png)\n\n![图1](https://ex.com/1.png)\n"
    t0 = time.perf_counter()
    out = await M.fix_markdown_images(md, client=None, target_id="G1", is_group=True, logger=L())
    dt = time.perf_counter() - t0
    print(f"  耗时 {dt:.2f}s（预算 {M._IMAGE_BUDGET_SECONDS}s）")
    print(f"  输出:\n{out}")
    ok_structure = out.count("![")==2
    ok_original = "https://ex.com/0.png" in out and "https://ex.com/1.png" in out
    print(f"  结构保持: {'✓' if ok_structure else '✗'}")
    print(f"  ★ 超时的图保留原地址（没被删、没被改坏）: {'✓' if ok_original else '✗'}")
    print(f"  ★ 按时返回（没被拖到 30s）: {'✓' if dt < 5 else '✗'}")
asyncio.run(main())
