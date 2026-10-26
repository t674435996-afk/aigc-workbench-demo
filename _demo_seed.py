# -*- coding: utf-8 -*-
"""演示数据灌注（联调模式，不调真实生图 API）。

目标：让演示态看起来**像跑了一阵子的平台**，而不是刚灌出来的脚本数据。三件事：

  ① 数据做厚 —— 3 个项目 / 7 份配方 / 90+ 张成品 / 60+ 轮次 / 38 行批量队列。
     页面空不是因为布局差，是因为只有 3 张卡。改数据比改界面管用得多。
     🔴 **配方收敛、每份覆盖多款** —— 4 份主模板就覆盖 54 款，其中相邻两套共享若干款
        （同一个款在不同模板下各出一张 ＝「效果数据回流 → 决定复用哪套」的落点）。
        一款一配方的话，配方就没有"跨款复用"这回事了。
  ② 时间戳打散 —— 配方几天前建 → 中间陆续出图 → 一周后回填效果数据，摊到过去 15 天
     （见 `_demo_timeline.py`）。所有记录都是"此刻"，是一眼看穿的假。
  ③ 三态齐全 —— 效果「好·可再用 / 一般 / 未回填」、批量「完成 / 失败 / 待生成」、
     成品「已入库 / 未沉淀」。全是成功、全是绿，同样是假。

⚠️ 运行前请先清空 `data/`（本脚本按"空库"灌）；脚本会**清空 `logs/operations.jsonl`** ——
   这是演示数据重置，不是生产日志。
"""
import base64
import datetime
import io
import json
import pathlib
import urllib.request

import _demo_timeline as tl

B = "http://127.0.0.1:8080"
EMP = "lin.design"
ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
LOGS = ROOT / "logs" / "operations.jsonl"
PHOTOS = ROOT / "photos" / "DEMO-01"
NOW = datetime.datetime.now().replace(microsecond=0)

PRODUCT_P = ("【产品细节】保持产品原样、颜色与版型不走样，面料质感与缝合细节清晰可见，"
             "领口 / 袖口 / 下摆结构完整，自然贴合模特身形，不遮挡不穿模")
ACTION_P = ("【动作构图】自然站姿或轻微走动，动作舒展不僵硬，构图居中留出留白，"
            "平视机位，突出人物与服装的整体关系")


def combo(person, scene, product, action=""):
    """组合提示词 = 形象 + 场景 + 产品 + 动作构图（与后端 compose_prompt 同一口径）。
    结构＝「什么人在什么场景穿着什么在做什么」。"""
    return "，".join([s.strip() for s in (person, scene, product, action) if s and s.strip()])

# plan：由本脚本收集，最后交给 _demo_timeline.apply() 统一改时间
PLAN = {"projects": {}, "recipes": {}, "assets": {}, "rounds": {}, "perf": {}, "logs": []}


# ---------------------------------------------------------------- 时间工具
def dt(days_ago, hh, mm=0):
    """N 天前的某个时刻（安全钳制：绝不落在未来）。
    hh / mm 允许超出常规范围（如 mm=92），由 timedelta 自动进位 —— 批量跑 40 行时会用到。"""
    base = (NOW - datetime.timedelta(days=days_ago)).replace(hour=0, minute=0, second=0, microsecond=0)
    d = base + datetime.timedelta(hours=hh, minutes=mm)
    if d > NOW - datetime.timedelta(minutes=6):
        d = NOW - datetime.timedelta(minutes=6 + (mm % 19))
    return d


def iso(d):
    return d.isoformat(timespec="seconds")


# ---------------------------------------------------------------- HTTP / 本地文件
def get(p):
    return json.load(urllib.request.urlopen(B + p))


def post(p, body):
    req = urllib.request.Request(B + p, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))


def _local(name):
    """读本地表（空库时该表可能还不存在 → 当作空列表）。"""
    p = DATA / name
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []


def _assets():
    return _local("assets.json")


def _rounds():
    return _local("rounds.json")


def _perf():
    return _local("performance.json")


def _loglines():
    return [ln for ln in LOGS.read_text(encoding="utf-8").splitlines() if ln.strip()] \
        if LOGS.exists() else []


def upload_bytes(pid, name, raw, purpose=""):
    body = {"project_id": pid, "name": name, "data": base64.b64encode(raw).decode()}
    if purpose:
        body["purpose"] = purpose
    return post("/api/upload_ref", body)


def upload_photo(pid, name, purpose=""):
    return upload_bytes(pid, name, (PHOTOS / name).read_bytes(), purpose)


def derived_ref_bytes():
    """在 3 张原图基础上派生 3 张（镜像 / 裁切 / 翻转）——
    让项目素材区有 6 张，而不是 3 张孤零零地摆在网格里。"""
    from PIL import Image, ImageOps
    files = sorted([p for p in PHOTOS.glob("*")
                    if p.suffix.lower() in (".jpg", ".jpeg", ".png")])
    if not files:
        return {}
    out = {}

    def enc(im, name):
        b = io.BytesIO()
        im.convert("RGB").save(b, format="JPEG", quality=92)
        out[name] = b.getvalue()

    a = Image.open(files[0])
    b = Image.open(files[1 % len(files)])
    c = Image.open(files[2 % len(files)])
    enc(ImageOps.mirror(a), "04_back.jpg")
    w, h = c.size
    enc(c.crop((int(w * .14), int(h * .14), int(w * .86), int(h * .86))), "05_detail.jpg")
    enc(ImageOps.flip(b), "06_pack.jpg")
    return out


# ---------------------------------------------------------------- 时间分配（核心）
def _assign(day, hour, a0, r0, l0, p0=None, rid="", spread="days"):
    """把一个生成组（支线 + 成品 + 轮次 + 日志）的时间摊到 day 天前那一天。

    spread="days"    —— 成品跨天（正常出图节奏：一天出一批）
    spread="minutes" —— 成品同一天、分钟级（批量跑：一次坐下来跑完）
    """
    new_a, new_r, new_l = _assets()[a0:], _rounds()[r0:], _loglines()[l0:]
    by = {}
    for a in new_a:
        by.setdefault(a["type"], []).append(a)

    # 支线：形象、场景同一天（并行），只差 1 分钟
    for a in by.get("base_person", []):
        PLAN["assets"][a["asset_id"]] = iso(dt(day, hour, 12))
    for a in by.get("base_scene", []):
        PLAN["assets"][a["asset_id"]] = iso(dt(day, hour, 13))

    # 镜头组：紧接两条支线之后，一次把 15 个动作跑出来（分钟级拉开 = 一次坐下来跑完）
    shot_ts = iso(dt(day, hour, 14))
    for i, a in enumerate(sorted(by.get("shot", []), key=lambda x: x.get("shot_index", 0))):
        t = iso(dt(day, hour, 14 + i))
        PLAN["assets"][a["asset_id"]] = t
        if i == 0:
            shot_ts = t

    # 成品：从「配方创建日」**线性摊到最近** ——
    #   一份视觉模板不是一天出完的：建完之后一批批陆续出（这周 3 款、下周 5 款…），
    #   一直到最近还在出。这才符合"配方是长期资产"的样子。
    #   spread="minutes" 则是「一次坐下来把这批跑完」（批量生成）。
    finals = by.get("final", [])
    n = len(finals)
    final_ts = []
    for i, a in enumerate(finals):
        if spread == "minutes":
            d_i, mm = day, 20 + i * 2
        else:
            d_i = max(day - int(round(i / (n - 1) * (day - 1))), 0) if n > 1 else max(day - 1, 0)
            mm = (i % 5) * 43
        t = iso(dt(d_i, hour, mm))
        PLAN["assets"][a["asset_id"]] = t
        final_ts.append(t)

    # 配方本身：第一张成品之后落库
    if rid:
        PLAN["recipes"][rid] = iso(dt(day, hour, 28))

    # 轮次：跟着它那一张资产走
    for r in new_r:
        aid = (r.get("asset_ids") or [""])[0]
        PLAN["rounds"][r["round_id"]] = PLAN["assets"].get(aid) or iso(dt(day, hour, 12))

    # 日志：按落地顺序对应（extract → gen_base×2 → compose×N）
    seq, ci, gb = [], 0, 0
    for ln in new_l:
        try:
            act = json.loads(ln).get("action", "")
        except Exception:  # noqa: BLE001
            act = ""
        if act == "extract":
            seq.append(iso(dt(day, hour, 8)))
        elif act == "gen_base":
            seq.append(iso(dt(day, hour, 12 if gb == 0 else 13)))
            gb += 1
        elif act == "gen_shots":
            seq.append(shot_ts)
        elif act == "compose":
            seq.append(final_ts[min(ci, len(final_ts) - 1)] if final_ts else iso(dt(day, hour, 20)))
            ci += 1
        else:
            seq.append(iso(dt(day, hour, 20)))
    PLAN["logs"].extend(seq)

    # 效果数据：导入时间 = 配方创建后 7 天（"先出图 → 一周后看数据"，顺序不能反）
    if p0 is not None:
        for x in _perf()[p0:]:
            PLAN["perf"][x["perf_id"]] = iso(dt(max(day - 7, 1), 10, 30))
    return by


# ---------------------------------------------------------------- 主链路：一份配方
def make_recipe(pid, name, skus, day, hour, person_hint="", scene_hint="", perf=None):
    """走一遍完整主链路：
    提取三段提示词 → 两条支线并行（形象 ‖ 场景）→ **生成镜头组（≈15 张 ＝ 视觉模板）**
    → 成品组合（组合词 = 形象 + 场景 + 产品）→ 落成视觉模板 → 后续成品复用同一份。
    """
    a0, r0, l0 = len(_assets()), len(_rounds()), len(_loglines())
    ex = post("/api/extract", {"project_id": pid, "employee_id": EMP,
                               "person_hint": person_hint, "scene_hint": scene_hint})
    pp = post("/api/gen_base", {"project_id": pid, "employee_id": EMP,
                                "kind": "person", "prompt": ex["person_prompt"]})["asset"]
    sp = post("/api/gen_base", {"project_id": pid, "employee_id": EMP,
                                "kind": "scene", "prompt": ex["scene_prompt"]})["asset"]
    # 镜头组：一个形象 × 一个场景 × 15 个动作 —— 这就是「视觉模板」的那组图
    shots = post("/api/gen_shots", {"project_id": pid, "employee_id": EMP,
                                    "person_prompt": ex["person_prompt"],
                                    "scene_prompt": ex["scene_prompt"],
                                    "person_asset_id": pp["asset_id"],
                                    "scene_asset_id": sp["asset_id"]})["shots"]
    product = ex.get("product_prompt") or PRODUCT_P
    action = ex.get("action_prompt") or ACTION_P
    rid = ""
    for i, sku in enumerate(skus):
        fin = post("/api/compose", {"project_id": pid, "employee_id": EMP, "sku": sku,
                                    "recipe_id": rid,
                                    "person_asset_id": pp["asset_id"],
                                    "scene_asset_id": sp["asset_id"],
                                    "person_prompt": ex["person_prompt"],
                                    "scene_prompt": ex["scene_prompt"],
                                    "product_prompt": product,
                                    "action_prompt": action})["asset"]
        if i == 0:
            rid = post("/api/recipe_save", {"project_id": pid, "employee_id": EMP, "name": name,
                                            "person_prompt": ex["person_prompt"],
                                            "scene_prompt": ex["scene_prompt"],
                                            "product_prompt": product,
                                            "action_prompt": action,
                                            "person_asset_id": pp["asset_id"],
                                            "scene_asset_id": sp["asset_id"],
                                            "final_asset_id": fin["asset_id"],
                                            "shots": [{"shot_index": s["shot_index"],
                                                       "action": s["action"],
                                                       "asset_id": s["asset"]["asset_id"]}
                                                      for s in shots],
                                            "params": {"sku": sku, "size": "2K",
                                                       "count": 1}})["recipe"]["recipe_id"]
    p0 = None
    if perf:
        p0 = len(_perf())
        post("/api/perf_import", {"employee_id": EMP, "rows": [
            {"recipe_id": rid, "period": pe, "ctr": c, "cvr": v, "sales": s}
            for pe, c, v, s in perf]})
    _assign(day, hour, a0, r0, l0, p0=p0, rid=rid)
    return {"recipe_id": rid, "person_asset_id": pp["asset_id"], "scene_asset_id": sp["asset_id"],
            "person_prompt": ex["person_prompt"], "scene_prompt": ex["scene_prompt"],
            "product_prompt": product, "action_prompt": action, "shot_count": len(shots)}


# ---------------------------------------------------------------- 自由出图（文生图 / 图生图）
def free_op(pid, prompt, day, hour, mm, ref=None):
    a0, r0, l0 = len(_assets()), len(_rounds()), len(_loglines())
    post("/api/gen_plain", {"project_id": pid, "employee_id": EMP, "refs": ([ref] if ref else []),
                            "prompt": prompt, "size": "2048x2048", "endpoint": "seedream"})
    ts = iso(dt(day, hour, mm))
    for a in _assets()[a0:]:
        if a["type"] in ("free_out", "free_in"):
            PLAN["assets"][a["asset_id"]] = ts
    for r in _rounds()[r0:]:
        PLAN["rounds"][r["round_id"]] = ts
    PLAN["logs"].extend([ts] * len(_loglines()[l0:]))


# ---------------------------------------------------------------- 批量生成（表格）
def batch_run(pid, rec, skus, day, hour, fail_rows=(), wait_skus=(), product=None):
    """跑一批：一份视觉模板 × 一批 SKU（≈40 行），另加失败行 / 待生成行（真实队列不会全绿）。
    组合词 = 模板的「形象 + 场景 + 动作」（固定） + 本行「产品」（可变）。"""
    a0, r0, l0 = len(_assets()), len(_rounds()), len(_loglines())
    P = rec.get("person_prompt", "") or ""
    Sc = rec.get("scene_prompt", "") or ""
    A = rec.get("action_prompt", "") or ACTION_P
    base = product or rec.get("product_prompt") or PRODUCT_P
    rows = []
    for sku in skus:
        r = post("/api/compose", {"project_id": pid, "employee_id": EMP, "sku": sku,
                                  "recipe_id": rec["recipe_id"],
                                  "person_asset_id": rec["person_asset_id"],
                                  "scene_asset_id": rec["scene_asset_id"],
                                  "person_prompt": P, "scene_prompt": Sc,
                                  "product_prompt": base, "action_prompt": A})
        a = r.get("asset") or {}
        rows.append({"sku": sku, "checked": True,
                     "status": "done" if (r.get("ok") or r.get("mock")) else "fail",
                     "secs": r.get("secs", ""), "asset_id": a.get("asset_id", ""),
                     "model": a.get("model_name", ""),
                     "prompt": a.get("prompt_used", "") or combo(P, Sc, base, A),
                     "product_prompt": base, "person_prompt": P, "scene_prompt": Sc,
                     "action_prompt": A, "err": ""})
    for sku, err in fail_rows:
        rows.append({"sku": sku, "checked": True, "status": "fail", "secs": "",
                     "asset_id": "", "model": "", "err": err,
                     "product_prompt": base, "person_prompt": P, "scene_prompt": Sc,
                     "action_prompt": A, "prompt": combo(P, Sc, base, A)})
    for sku in wait_skus:
        rows.append({"sku": sku, "checked": True, "status": "wait", "secs": "",
                     "asset_id": "", "model": "", "err": "",
                     "product_prompt": base, "person_prompt": P, "scene_prompt": Sc,
                     "action_prompt": A, "prompt": combo(P, Sc, base, A)})
    _assign(day, hour, a0, r0, l0, spread="minutes")
    post("/api/batch_save", {"project_id": pid, "rows": rows})


def tag_refs(pid, ts):
    """把某个项目的参考图（＝要上架的款）统一打到"导入那天"。"""
    for a in _assets():
        if a["project_id"] == pid and a["type"] == "ref":
            PLAN["assets"][a["asset_id"]] = ts


# ---------------------------------------------------------------- 成品质检状态
def spread_qc(pid, pending_n=6, fail_every=23):
    """把成品的确认状态铺成三态 —— 多数「已确认」/ 最近一小撮「待挑选」/ 个别「需重做」。

    🔴 为什么必须铺：「成品挑选」是**批量结果的收尾**，真实平台永远有一小撮刚跑完
       还没挑的图堆在那儿。全标 ok → 这页空着没事干；全标 pending → 满屏待挑，一样是假。

    ⚠️ 必须在 `tl.apply()` **之后**调用 —— 它按 `created_at` 排序来挑"最近的几张"。
    """
    ar = [a for a in _local("assets.json")
          if a.get("project_id") == pid and a.get("type") == "final"]
    ar.sort(key=lambda x: x.get("created_at", ""), reverse=True)   # 新的在前
    cnt = {"ok": 0, "fail": 0, "pending": 0}
    for i, a in enumerate(ar):
        if i < pending_n:
            st = "pending"                    # 最近这几张：刚跑完，还没挑
        elif i % fail_every == 0:
            st = "fail"                       # 个别需要重做
        else:
            st = "ok"
        post("/api/asset_mark", {"asset_id": a["asset_id"], "qc_status": st})
        cnt[st] += 1
    print(f"   质检状态：已确认 {cnt['ok']} / 待挑选 {cnt['pending']} / 需重做 {cnt['fail']}")


# ---------------------------------------------------------------- 素材文案
STUDIO_P = "正面直立 A-pose 素模特，肤色偏白、身形匀称，只穿肤色贴身衣物、无遮挡，全身完整入镜，四肢自然下垂，五官自然真实，不佩戴任何配饰"

# 项目一：**少数几套视觉模板 × 多款**
# 🔴 关键不是"跑满多少份"，而是**配方是产出单元**：
#    · 每套模板覆盖一批款（而不是一款一配方 —— 那样配方就没有复用价值）
#    · 相邻两套模板**共享若干款** → 同一个款在不同模板下各出一张
#      ＝ 效果数据回流后「决定这个款以后复用哪套模板」的落点
P1 = [
    ("春季极简棚拍白模特", 26, 10,
     [f"KN-{1001 + i}-A" for i in range(16)],
     STUDIO_P,
     "简洁室内摄影棚，纯白无缝背景，柔和顺光，地面干净，适合服装展示，无多余道具",
     [("近30天", "0.072", "0.031", 1280), ("上一周期", "0.064", "0.028", 1090)]),
    ("秋季英伦街头黑模特", 19, 14,
     [f"KN-{1009 + i}-A" for i in range(16)],        # ← 与上一套**重叠 8 款**
     "正面直立 A-pose 素模特，深肤色、短发、身形挺拔，只穿肤色贴身衣物、无遮挡，全身完整入镜，五官与身形自然真实",
     "伦敦街区砖墙边，阴天漫射光，暖灰调街景，路面干净通透，适合服装展示",
     [("近30天", "0.061", "0.027", 1040)]),
    ("夏季通勤简约亚裔模特", 12, 11,
     [f"KN-{1017 + i}-A" for i in range(14)],        # ← 与上一套**重叠 8 款**
     "正面直立 A-pose 素模特，亚裔面孔、黑色长直发、身形纤细，只穿肤色贴身衣物、无遮挡，全身完整入镜，五官自然真实",
     "城市写字楼外立面，浅灰色混凝土墙面，上午自然侧光，空间通透不杂乱，适合服装展示",
     [("近30天", "0.052", "0.022", 880)]),
    ("冬季室内暖光白模特", 5, 15,
     [f"KN-{1022 + i}-A" for i in range(8)],         # ← 与前两套都重叠
     "正面直立 A-pose 素模特，肤色偏白、浅棕长发，只穿肤色贴身衣物、无遮挡，全身完整入镜，姿态放松自然",
     "室内复古客厅，米色墙纸与木质护墙板，暖色灯光，浅景深，空间通透",
     None),                                          # ← 效果数据「未回填」那一种状态
]


def main():
    # ---- 0. 重置日志（演示数据重置，不是生产日志） ----
    LOGS.parent.mkdir(parents=True, exist_ok=True)
    LOGS.write_text("", encoding="utf-8")

    p1 = get("/api/state?project_id=")["projects"][0]["project_id"]
    PLAN["projects"][p1] = iso(dt(15, 9, 0))
    print("项目一:", p1)

    # ---- 项目一的参考图：启动种子已导 3 张，再派生 3 张 ----
    for nm, raw in derived_ref_bytes().items():
        upload_bytes(p1, nm, raw)
    # ------------------------------------------------------------------ 项目一
    print("项目一 配方：")
    recs = []
    for name, day, hour, skus, ph, sh, *perf in P1:
        r = make_recipe(p1, name, skus, day, hour, person_hint=ph, scene_hint=sh,
                        perf=(perf[0] if perf else None))
        recs.append(r)
        print(f"   · {name} | {len(skus)} 个 SKU | {day} 天前")

    # 批量生成：一份视觉模板 × 一批 40 个 SKU（真实批次量）；另加 2 行失败 / 4 行待生成
    # ⚠️ 挂在 recs[0]（＝页面上默认选中的第一份），打开批量页就能直接对上，不用先切下拉
    batch_run(p1, recs[0], [f"KN-11{i:02d}-S" for i in range(1, 35)], 1, 15,
              fail_rows=[("KN-1135-S", "上游模型超时（120s 未返回）"),
                         ("KN-1136-S", "参考图分辨率不足（短边 < 800px）")],
              wait_skus=["KN-1137-S", "KN-1138-S", "KN-1139-S", "KN-1140-S"])

    # 自由出图：先文生图，再图生图（同一张素材）
    free_op(p1, "简约室内场景，浅色墙面，柔和自然光，画面干净无杂物", 2, 11, 20)
    src0 = sorted([p for p in PHOTOS.glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png")])
    if src0:
        up = upload_bytes(p1, "free_src.jpg", src0[0].read_bytes(), purpose="free")
        PLAN["assets"][up["asset"]["asset_id"]] = iso(dt(1, 16, 38))
        free_op(p1, "保留产品原样不变形，背景换成浅灰、柔和侧光，画面干净", 1, 16, 40,
                ref=up["asset"]["asset_id"])

    tag_refs(p1, iso(dt(15, 9, 5)))

    # ------------------------------------------------------------------ 项目二 / 三
    p2 = post("/api/project_create", {"name": "24SS 春夏预览", "employee_id": EMP})["project"]["project_id"]
    PLAN["projects"][p2] = iso(dt(8, 9, 30))
    for nm in ["01_front.jpg", "02_side.jpg", "03_angle.jpg"]:
        upload_photo(p2, nm)
    tag_refs(p2, iso(dt(8, 9, 35)))
    print("项目二:", p2)
    make_recipe(p2, "夏季海边白纱长裙白模特", ["KN-2001-A", "KN-2002-A"], 6, 11,
                person_hint="正面直立 A-pose 素模特，肤色偏白、长直发，只穿肤色贴身衣物、无遮挡，全身完整入镜",
                scene_hint="海边礁石与浅滩，晴天侧逆光，远处海平线通透，风感自然")
    make_recipe(p2, "春季街头休闲亚裔模特", ["KN-2101-A", "KN-2102-A"], 2, 14,
                person_hint="正面直立 A-pose 素模特，亚裔面孔、马尾发型，只穿肤色贴身衣物、无遮挡，全身完整入镜",
                scene_hint="老城区步行街，浅色水泥墙与绿植，下午自然光，空间不杂乱")

    p3 = post("/api/project_create", {"name": "25SS 打样测试", "employee_id": EMP})["project"]["project_id"]
    PLAN["projects"][p3] = iso(dt(2, 17, 0))
    for nm in ["03_angle.jpg", "02_side.jpg"]:
        upload_photo(p3, nm)
    tag_refs(p3, iso(dt(2, 17, 5)))
    print("项目三:", p3)
    make_recipe(p3, "冬季室内暖光白模特", ["KN-3001-A"], 1, 10,
                person_hint="正面直立 A-pose 素模特，肤色偏白、浅色短发，只穿肤色贴身衣物、无遮挡，全身完整入镜",
                scene_hint="室内暖光客厅，米色地毯与木质家具，暖黄落地灯，浅景深")

    # ---- 落时间 ----
    rep = tl.apply(PLAN)
    print("\n时间轴已摊开：")
    for k in ("projects.json", "assets.json", "recipes.json", "performance.json", "rounds.json"):
        print(f"   {k:<18} 改写 {rep.get(k, 0)} 条")
    print(f"   operations.jsonl   改写 {rep.get('logs_aligned', 0)}/{rep.get('logs', 0)} 条")

    # ---- 成品质检状态（必须在落时间**之后**：按 created_at 挑"最近几张"留待挑选）----
    print("\n成品质检：")
    spread_qc(p1)
    spread_qc(p2, pending_n=1, fail_every=99)
    spread_qc(p3, pending_n=1, fail_every=99)

    # ---- 摘要 ----
    st = get("/api/state?project_id=" + p1)
    print(f"\n演示态就绪｜项目一 {p1}")
    print("  配方:", len(st["recipes"]), "| 轮次:", len(st["rounds"]),
          "| 成品:", len([a for a in st["assets"] if a["type"] == "final"]),
          "| 参考图:", len([a for a in st["assets"] if a["type"] == "ref"]))
    for r in st["recipes"]:
        s = r["stats"]
        print(f"   - {r['name']:<18} 成品 {s['final_count']} 张 | {s['sku_count']} 个 SKU | "
              f"效果 {s['perf_count']} 条 | 建于 {r['created_at'][:16]}")
    print("\n全部项目:")
    for p in get("/api/projects"):
        print(f"   {p['project_id']}  {p['name']}  建于 {p['created_at'][:16]}")


if __name__ == "__main__":
    main()
