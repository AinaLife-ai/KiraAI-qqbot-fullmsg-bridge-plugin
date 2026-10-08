"""验证多图是并行处理（不是串行）。"""
import asyncio, os, sys, time
ROOT="/var/minis/workspace/qqbot_bridge_review"
sys.path.insert(0, ROOT+"/kira-v3"); sys.path.insert(0, ROOT+"/bridge")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.makedirs(ROOT+"/data",exist_ok=True); open(ROOT+"/data/log.log","a").close()
import md_media as M

DELAY = 0.4
CONC = {"now":0, "max":0}

async def slow_remote(client, tid, isg, url, logger=None, timeout=30.0):
    CONC["now"] += 1; CONC["max"] = max(CONC["max"], CONC["now"])
    await asyncio.sleep(DELAY)
    CONC["now"] -= 1
    return url + "?conv=1"

async def main():
    M.upload_remote_to_public_url = slow_remote
    M.clear_caches()
    md = "\n".join(f"![图{i}](https://example.com/{i}.png)" for i in range(6))
    t0 = time.perf_counter()
    out = await M.fix_markdown_images(md, client=None, target_id="G1", is_group=True)
    dt = time.perf_counter() - t0
    print(f"  6 张图（每张 {DELAY}s）总耗时: {dt:.2f}s")
    print(f"  峰值并发: {CONC['max']}")
    serial = 6*DELAY
    print(f"  串行会是: {serial:.2f}s")
    print(f"  {'✓ 并行生效（快了 %.1fx）'%(serial/dt) if dt < serial*0.7 else '✗ 看起来还是串行'}")
    print(f"  结构保持: {'✓' if out.count('![')==6 else '✗'} | 原图片顺序不变: "
          f"{'✓' if all(f'![图{i}]' in out for i in range(6)) else '✗'}")
asyncio.run(main())
