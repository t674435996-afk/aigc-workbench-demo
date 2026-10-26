# -*- coding: utf-8 -*-
"""
AIGC 出图工作台 v2（复现版）
============================
按「项目 / 素材 / 配方 / 效果数据」四层重建主出图链路：

  建项目 → 导入参考图 → 提取提示词
        → 并行出【支线A 模特基础照】与【支线B 初版场景】
        → 组合生成成品（款 × 形象 × 场景）
        → 保存入库（落成一份"配方"）→ 效果数据回流到配方；批量出的图在批量页逐行「标记合格」

设计原则（对应 B2 阶段）：
  · **数据模型先行**：表结构 = 《出图平台复现_数据模型_v1.md》的代码落地
  · **界面先跑通、后美化**：本阶段只证"链能走完"，不调视觉
  · **无 key 降级**：--mock 或未配 key 时，生图只留 prompt 与占位，链路仍完整可走

启动:
  python aigc_workbench.py [--port 8080] [--mock]

依赖: 仅标准库；真实生图需 requests，QC 打分需 opencv（缺失自动跳过）。
"""

import argparse
import base64
import contextlib
import csv
import datetime
import io
import itertools
import json
import mimetypes
import os
import re
import shutil
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------- 目录与全局 ----------------
BASE = Path(__file__).parent
INDEX = BASE / "aigc_workbench.html"
DATA = BASE / "data"
FILES = DATA / "files"                 # 所有图片落这里（参考图 / 基础照 / 成品）
UPLOADS = DATA / "uploads"
GEN_DIR = FILES                        # 生成产物直接进 files/
LOGS = BASE / "logs" / "operations.jsonl"
PHOTOS = BASE / "photos"               # 老素材池（种子用）
TEMPLATES = BASE / "templates"

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# 联调模式开关：True 时界面顶部显示「联调模式 · 未调用模型 API」徽标，
# 并且**耗时列显示 —**（联调本来就不产生真实耗时，写 0s 一眼假）
MOCK_MODE = False

# 启动时加载 .env（先自己目录，再上一级），避免每次手动 export
for _env in (BASE / ".env", BASE.parent / ".env"):
    if _env.exists():
        for line in _env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

import importlib
gen = importlib.import_module("aigc_gen_tool")   # 复用生成引擎（多端点 + mock 降级）

# 四个端点 key 的名字。切换模式时只在这四个属性上做文章。
KEY_NAMES = ("OPENAI_API_KEY", "GEMINI_API_KEY", "ARK_API_KEY", "ARTSAPI_API_KEY")


def _reload_env() -> None:
    """重新读 .env 覆盖 os.environ —— 为了**不重启**也能认到刚补进去的 key。
    ⚠️ 这里用覆盖（非 setdefault）：运行中改了 .env，切一次模式就生效。"""
    for _env in (BASE / ".env", BASE.parent / ".env"):
        if _env.exists():
            for line in _env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()


def key_status() -> dict:
    """每个端点 key 是否有值（看**真实环境**，与当前 mock 开关无关）。"""
    _reload_env()
    return {k: bool(os.environ.get(k, "").strip()) for k in KEY_NAMES}


def set_mock(on: bool) -> dict:
    """🔴 运行中切换「正式 / 联调」—— 演示现场不用停服务、不用改启动参数。

    on=True ：清空引擎上的四个 key → 全链路出**占位图**，不联网、不计费、零等待。
    on=False：从环境（含 .env）**重新装回** key → 恢复真实调用。

    🔑 设计要点：只改 `gen` 模块的属性，**不动 os.environ** —— 所以切回去随时能还原；
    也正因为如此，`--mock` 启动过的进程一样能切回正式（key 一直躺在环境里）。
    ⚠️ 只影响**之后**发起的调用，已在跑的那一次请求不受影响。
    """
    global MOCK_MODE
    MOCK_MODE = bool(on)
    _reload_env()
    for k in KEY_NAMES:
        setattr(gen, k, "" if MOCK_MODE else os.environ.get(k, "").strip())
    ks = key_status()
    ready = [k for k in KEY_NAMES if ks[k]]
    if MOCK_MODE:
        note = "已切到联调模式：出占位图，不调用 API、不产生费用"
    elif ready:
        note = "已切到正式模式：生成会真实调用 API 并按张计费"
    else:
        note = "已切到正式模式，但没有任何可用 key → 仍会出占位图"
    print(f"[mode] {'联调模式（不调 API）' if MOCK_MODE else '正式模式'}"
          f" | 可用 key: {', '.join(ready) if ready else '无'}")
    return {"ok": True, "mock_mode": MOCK_MODE, "keys": ks, "note": note}


def force_mock() -> None:
    """启动参数 --mock：等价于开局就把开关拨到联调。"""
    set_mock(True)


# ---------------- JSON 存储（四张表 + 日志） ----------------
def _load(name: str, default):
    p = DATA / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return default
    return default


def _save(name: str, obj) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


_SEQ = itertools.count(1)   # 进程内自增序号：保证同一毫秒内连续建的多条记录 id 不撞


def _nid(prefix: str) -> str:
    return f"{prefix}{int(time.time() * 1000) % 10 ** 10}{next(_SEQ) % 1000:03d}"


def log_op(action: str, project_id: str = "", employee_id: str = "", prompt: str = "",
           model: str = "", gen_count: int = 0, img_count: int = 0, extra: dict = None) -> None:
    """写入操作日志（对应 op_logs 表：员工id / 生成时间 / 次数 / 张数 / 提示词 / 模型）。"""
    try:
        LOGS.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": _now(), "employee_id": employee_id, "project_id": project_id,
               "action": action, "gen_count": gen_count, "img_count": img_count,
               "prompt_content": (prompt or "").replace("\n", " ")[:160],
               "model_name": model}
        if extra:
            row.update(extra)
        with open(LOGS, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def list_logs(limit: int = 200) -> list:
    rows = []
    if LOGS.exists():
        for line in LOGS.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
    return rows[-limit:][::-1]


# ---------------- ① projects 项目 ----------------
def list_projects() -> list:
    return _load("projects.json", [])


def create_project(body: dict) -> dict:
    name = str(body.get("name", "")).strip() or "未命名项目"
    emp = str(body.get("employee_id", "")).strip() or "EMP-001"
    rows = list_projects()
    p = {"project_id": _nid("p"), "employee_id": emp, "name": name, "created_at": _now()}
    rows.append(p)
    _save("projects.json", rows)
    return {"ok": True, "project": p}


# ---------------- ② assets 素材（输入 + 产出，靠 type 分） ----------------
def list_assets(project_id: str = "") -> list:
    rows = _load("assets.json", [])
    if project_id:
        rows = [r for r in rows if r.get("project_id") == project_id]
    return sorted(rows, key=lambda r: r.get("created_at", ""))


def _add_asset(project_id, atype, fname, **kw) -> dict:
    rows = _load("assets.json", [])
    a = {"asset_id": _nid("a"), "project_id": project_id, "recipe_id": kw.get("recipe_id", ""),
         "type": atype, "file": fname, "sku": kw.get("sku", ""),
         "prompt_used": kw.get("prompt_used", ""), "model_name": kw.get("model_name", ""),
         "gen_seconds": kw.get("gen_seconds", 0), "qc_status": kw.get("qc_status", "na"),
         "saved": bool(kw.get("saved", False)), "created_at": _now(),
         # 参考图角色（提取提示词用）：model=模特参考图 / scene=场景参考图 / product=上架产品图；""＝普通项目素材
         "ref_role": kw.get("ref_role", ""),
         # 视觉模板「镜头组」用（type=shot）：这个镜头是第几个、动作是什么
         "shot_index": kw.get("shot_index", 0), "action_prompt": kw.get("action_prompt", ""),
         # 成品图的三段来源（组合词 = 形象 + 场景 + 产品）
         "person_prompt": kw.get("person_prompt", ""), "scene_prompt": kw.get("scene_prompt", ""),
         "product_prompt": kw.get("product_prompt", "")}
    rows.append(a)
    _save("assets.json", rows)
    return a


def mark_asset(body: dict) -> dict:
    aid = str(body.get("asset_id", ""))
    rows = _load("assets.json", [])
    for r in rows:
        if r["asset_id"] == aid:
            if "saved" in body:
                r["saved"] = bool(body["saved"])
            if "qc_status" in body:
                r["qc_status"] = str(body["qc_status"])
            _save("assets.json", rows)
            return {"ok": True, "asset": r}
    return {"ok": False, "error": "asset not found"}


def delete_asset(body: dict) -> dict:
    """删除一张**导入类**素材（参考图 type=ref ／ 自由出图输入图 type=free_in），连带删磁盘文件。

    🔴 只允许删这两类 —— 支线(base_*)/镜头(shot)/成品(final) 是链路的产物，
    配方可能引用它们，删了会把「复用」打断。"""
    aid = str(body.get("asset_id", ""))
    rows = _load("assets.json", [])
    keep, victim = [], None
    for r in rows:
        if r.get("asset_id") == aid:
            victim = r
        else:
            keep.append(r)
    if not victim:
        return {"ok": False, "error": "asset not found"}
    if victim.get("type") not in ("ref", "free_in"):
        return {"ok": False, "error": f"仅可删除导入类素材（当前 type={victim.get('type')}）"}
    _save("assets.json", keep)
    f = victim.get("file")
    if f:
        try:
            (FILES / f).unlink()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "deleted": aid}


# ---------------- ③ recipes 配方（＝视觉模板） ----------------
def list_recipes() -> list:
    return _load("recipes.json", [])


def save_recipe(body: dict) -> dict:
    rows = list_recipes()
    r = {"recipe_id": _nid("r"), "name": str(body.get("name", "")).strip() or "未命名配方",
         "project_id": str(body.get("project_id", "")),
         # 视觉模板的四段提示词 —— 组合生成时 = 形象 + 场景 + 产品 + 动作构图
         "person_prompt": str(body.get("person_prompt", "")),
         "scene_prompt": str(body.get("scene_prompt", "")),
         "product_prompt": str(body.get("product_prompt", "")),
         "action_prompt": str(body.get("action_prompt", "")),
         "person_asset_id": str(body.get("person_asset_id", "")),
         "scene_asset_id": str(body.get("scene_asset_id", "")),
         "final_asset_id": str(body.get("final_asset_id", "")),
         # 视觉模板 = 多张图（≈15）。一镜 ＝ {shot_index, action, asset_id}
         "shots": body.get("shots") or [],
         "params": body.get("params") or {},
         "created_by": str(body.get("employee_id", "")),
         "created_at": _now(), "visibility": "team"}
    rows.append(r)
    _save("recipes.json", rows)
    # 把这条配方出的成品挂上 recipe_id，并把三条资产标记为已入库
    ar = _load("assets.json", [])
    changed = False
    shot_ids = {str(s.get("asset_id", "")) for s in (r.get("shots") or [])}
    for a in ar:
        if a["asset_id"] == r["final_asset_id"]:
            a["recipe_id"] = r["recipe_id"]; a["saved"] = True
            # 🔴 保存入库＝作者对**这一张**的确认 → 顺带标 ok（不必再去批量页标记它）
            a["qc_status"] = "ok"
            changed = True
        elif a["asset_id"] in (r["person_asset_id"], r["scene_asset_id"]) or a["asset_id"] in shot_ids:
            a["saved"] = True; changed = True
    if changed:
        _save("assets.json", ar)
    return {"ok": True, "recipe": r}


# ---------------- ③b 配方派生统计与详情（B3 素材库） ----------------
def _recipe_stats(rid: str) -> dict:
    """一份配方的派生统计：出过几张成品 / 跨几个款 / 有没有效果数据 / 最近一次生成时间。
    ⛔ 不落盘 —— 全部从 assets + performance 现算，避免两处漂移。"""
    finals = [a for a in _load("assets.json", [])
              if a.get("recipe_id") == rid and a.get("type") == "final"]
    perf = [p for p in list_perf() if p.get("recipe_id") == rid]
    skus = sorted({a.get("sku", "") for a in finals if a.get("sku")})
    return {"final_count": len(finals),
            "sku_count": len(skus),
            "skus": skus,
            "perf_count": len(perf),
            "last_at": max([a.get("created_at", "") for a in finals] or [""])}


def recipes_with_stats(project_id: str = "") -> list:
    """列表用：原始配方 + stats（前端排序/筛选靠它，不必逐条拉详情）。
    传 project_id → 只返回该项目的配方（素材库跟着当前项目走，多项目下才不串台）。"""
    rows = list_recipes()
    if project_id:
        rows = [r for r in rows if r.get("project_id") == project_id]
    out = []
    for r in rows:
        r2 = dict(r)
        r2["stats"] = _recipe_stats(r["recipe_id"])
        out.append(r2)
    return out


def get_recipe_detail(rid: str) -> dict:
    """配方详情：这份配方**出过的所有图** + 支线素材 + 效果数据明细。
    🔑 素材库"点开一份配方"看到的就是它 —— 证明配方是产出单元，不是一张图。"""
    rec = next((r for r in list_recipes() if r["recipe_id"] == rid), None)
    if not rec:
        return {"ok": False, "error": "recipe not found"}
    assets = _load("assets.json", [])
    key = lambda a: a.get("created_at", "")  # noqa: E731
    finals = sorted([a for a in assets
                     if a.get("recipe_id") == rid and a.get("type") == "final"], key=key)
    persons = [a for a in assets if a.get("asset_id") == rec.get("person_asset_id")]
    scenes = [a for a in assets if a.get("asset_id") == rec.get("scene_asset_id")]
    shot_ids = [str(s.get("asset_id", "")) for s in (rec.get("shots") or [])]
    by_id = {a["asset_id"]: a for a in assets}
    shots = [{"shot_index": s.get("shot_index", i + 1), "action": s.get("action", ""),
              "asset": by_id.get(str(s.get("asset_id", "")))}
             for i, s in enumerate(rec.get("shots") or [])]
    perf = sorted([p for p in list_perf() if p.get("recipe_id") == rid],
                  key=lambda p: p.get("imported_at", ""))
    return {"ok": True, "recipe": rec, "finals": finals, "persons": persons, "scenes": scenes,
            "shots": shots, "shot_count": len(shot_ids),
            "performance": perf, "stats": _recipe_stats(rid)}


# ---------------- ④ performance 效果数据（挂配方，不挂单图） ----------------
def list_perf() -> list:
    return _load("performance.json", [])


def import_perf(body: dict) -> dict:
    """导入外部效果数据。body.rows = [{recipe_id, period, ctr, cvr, sales}]"""
    rows = body.get("rows") or []
    emp = str(body.get("employee_id", ""))
    store = list_perf()
    n = 0
    for r in rows:
        rid = str(r.get("recipe_id", ""))
        if not rid:
            continue
        store.append({"perf_id": _nid("f"), "recipe_id": rid, "period": str(r.get("period", "")),
                      "ctr": r.get("ctr", ""), "cvr": r.get("cvr", ""), "sales": r.get("sales", ""),
                      "source": "外部系统导出", "imported_by": emp, "imported_at": _now()})
        n += 1
    _save("performance.json", store)
    return {"ok": True, "imported": n}


# ---------------- ⑤ rounds 轮次记录（右侧"每轮出图"流水，供逐轮对比） ----------------
# 每完成一次生成动作 = 一条轮次。kind 取值：
#   person / scene → 出图链路的支线   final → 组合成品   free → 自由出图（文生图·图生图）
def list_rounds(project_id: str = "", kind: str = "", limit: int = 80) -> list:
    rows = _load("rounds.json", [])
    if project_id:
        rows = [r for r in rows if r.get("project_id") == project_id]
    if kind == "chain":
        rows = [r for r in rows if r.get("kind") in ("person", "scene", "shot", "final")]
    elif kind == "free":
        rows = [r for r in rows if r.get("kind") == "free"]
    return rows[-limit:][::-1]


def add_round(project_id: str, employee_id: str, kind: str, prompt: str,
              model: str, asset_ids: list, extra: dict = None) -> dict:
    rows = _load("rounds.json", [])
    r = {"round_id": _nid("rd"), "project_id": project_id, "employee_id": employee_id,
         "kind": kind, "prompt": (prompt or "").replace("\n", " ")[:200],
         "model": model, "asset_ids": [a for a in (asset_ids or []) if a], "ts": _now()}
    if extra:
        r.update(extra)
    rows.append(r)
    _save("rounds.json", rows)
    return r


def list_free_inputs(project_id: str = "") -> list:
    """自由出图用过的输入图（type=free_in）—— 与出图链路的参考图完全隔离。"""
    rows = [a for a in _load("assets.json", []) if a.get("type") == "free_in"]
    if project_id:
        rows = [r for r in rows if r.get("project_id") == project_id]
    return sorted(rows, key=lambda r: r.get("created_at", ""))


# ---------------- ⑥ 批量生成任务行（持久化 —— 刷新页面不丢队列） ----------------
def get_batch_rows(project_id: str = "") -> list:
    return _load("batch.json", {}).get(project_id, []) if project_id else []


def save_batch_rows(body: dict) -> dict:
    pid = str(body.get("project_id", ""))
    if not pid:
        return {"ok": False, "error": "缺少 project_id"}
    keep = []
    for r in (body.get("rows") or []):
        keep.append({
            "sku": str(r.get("sku", "")),
            "checked": bool(r.get("checked", True)),
            # 「生成中」不落盘 —— 刷新后回到「待生成」，不会卡在假进度
            "status": "wait" if r.get("status") == "run" else str(r.get("status", "wait")),
            "secs": r.get("secs", ""), "asset_id": str(r.get("asset_id", "")),
            "err": str(r.get("err", "")), "model": str(r.get("model", "")),
            "prompt": str(r.get("prompt", "")),
            # 逐行产品词（组合词的可变部分）＋ 该行跑的时候用的固定几段（回看时不串台）
            "product_prompt": str(r.get("product_prompt", "")),
            "person_prompt": str(r.get("person_prompt", "")),
            "scene_prompt": str(r.get("scene_prompt", "")),
            "action_prompt": str(r.get("action_prompt", "")),
        })
    store = _load("batch.json", {})
    store[pid] = keep
    _save("batch.json", store)
    return {"ok": True, "count": len(keep)}


# ---------------- 生图：一个入口收敛多端点 ----------------
_MODEL_NOISE = re.compile(r"\s*[（(][^）)]*(?:无\s*key|mock|artsapi|联调)[^）)]*[）)]\s*", re.I)


def _clean_model(name: str) -> str:
    """模型名去掉内部的联调/网关痕迹（如 `xxx(artsapi:无key)`），只留真实模型名。
    联调状态由界面顶部徽标统一说明 —— 不在模型名里重复十几遍。"""
    return _MODEL_NOISE.sub("", str(name or "")).strip()


def _run_image(prompt: str, refs: list, out_stem: str, template: str = "",
               size: str = "2K", endpoint: str = "seedream", model: str = "",
               kind: str = "") -> dict:
    """调用生成引擎出单张（或多张）。返回 {ok, paths, model, secs, mock, error}。
    kind＝资产类型（base_person/base_scene/final/free_out）：联调态据此给占位图上色，
    使主界面三卡并排时能一眼区分。"""
    FILES.mkdir(parents=True, exist_ok=True)
    out_path = GEN_DIR / f"{out_stem}.png"
    preset = {"model": model or {"seedream": "doubao-seedream-5-0-pro-260628",
                                 "gemini": "gemini-2.5-flash-image",
                                 "openai": "gpt-image-1"}.get(endpoint, "doubao-seedream-5-0-pro-260628"),
              "endpoint": endpoint, "size": size,
              "ref_images": max(len(refs), 1), "output": "1"}
    t0 = time.time()
    try:
        if endpoint == "gemini":
            r = gen.gen_gemini(preset, refs, prompt, out_path, template, kind=kind)
        elif endpoint == "openai":
            r = gen.gen_openai(preset, refs, prompt, out_path, kind=kind)
        else:
            r = gen.gen_seedream(preset, refs, prompt, out_path, template, kind=kind)
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "paths": [], "error": str(e)[:200], "model": preset["model"]}
    r["secs"] = round(time.time() - t0, 1)
    r["model"] = _clean_model(r.get("model", ""))
    r["out_path"] = str(out_path)
    return r


EXTRACT_PERSON = ("正面直立 A-pose 素模特，只穿贴身衣物、无遮挡，纯白背景，自然柔光，"
                  "全身完整入镜，五官与身形自然真实，不戴任何配饰")
EXTRACT_SCENE = ("简洁室内摄影棚场景，浅色背景，柔和自然光，空间通透不杂乱，"
                 "地面干净，适合服装展示，无多余道具")
EXTRACT_PRODUCT = ("【产品细节】保持产品原样、颜色与版型不走样，面料质感与缝合细节清晰可见，"
                   "领口 / 袖口 / 下摆等结构完整，自然贴合模特身形，不遮挡不穿模")
EXTRACT_ACTION = ("【动作构图】自然站姿或轻微走动，动作舒展不僵硬，构图居中留出留白，"
                  "平视机位，突出人物与服装的整体关系，画面干净不抢戏")

def compose_prompt(person: str, scene: str, product: str, action: str = "") -> str:
    """🔴 组合生成提示词 = **形象词 + 场景词 + 产品词 + 动作构图词**（四段真拼）。
    结构＝「**什么人在什么场景穿着什么在做什么**」：
        形象(什么人) → 场景(什么场景) → 产品(穿着什么) → 动作(做什么)
    ⛔ 不是写死的一句话 —— 这是「组合生成」与「单图裸跑」的分界。"""
    segs = [s.strip() for s in (person, scene, product, action) if s and str(s).strip()]
    return "，".join(segs)


# 🔴 提示词识别的指令：**每个区块一个指令、各读各的图**（不再一把梭读所有图反推四段）。
#    这样产品图就只反推「产品词」，不会被模特图里的衣服带偏（治品类错配）。
EXTRACT_INST_PERSON = (
    "你是服装视觉素材的提示词工程师。请阅读这张**模特参考图**，"
    "输出一段「形象提示词」（30–50 字，只描述画面本身）："
    "模特长什么样——性别、年龄感、身形、发型、妆容、姿态（A-pose 素模特，不含服装款式）。\n"
    "只输出这段提示词本身，不要前缀、不要解释、不要多余内容。"
)
EXTRACT_INST_SCENE = (
    "你是服装视觉素材的提示词工程师。请阅读这张**场景参考图**，"
    "输出一段「场景提示词」（30–50 字，只描述画面本身）："
    "在什么环境拍摄——场地、背景、光线、氛围（不含人物、不含服装款式）。\n"
    "只输出这段提示词本身，不要前缀、不要解释、不要多余内容。"
)
EXTRACT_INST_PRODUCT = (
    "你是电商视觉素材的提示词工程师。请阅读这张**产品图**，"
    "输出一段「产品提示词」（30–50 字，只描述画面本身）："
    "要上架的这件商品长什么样——品类、颜色、材质 / 面料、版型 / 结构、工艺细节，"
    "如实还原参考图、不要凭想象添加（不管是衣服、鞋还是配饰，照图描述）。\n"
    "只输出这段提示词本身，不要前缀、不要解释、不要多余内容。"
)
EXTRACT_INST_ACTION = (
    "你是服装视觉素材的提示词工程师。请阅读这张**模特参考图**，"
    "输出一段「动作构图提示词」（30–50 字，只描述画面本身）："
    "模特在做什么动作、什么姿势、什么机位与构图——站姿 / 坐姿 / 走动、镜头角度、景别、"
    "画面留白与人物位置（只描述动作与构图，不含外貌、不含服装款式）。\n"
    "只输出这段提示词本身，不要前缀、不要解释、不要多余内容。"
)


def _parse_four(text: str) -> dict:
    """从视觉模型返回文本里解析四段；解析不出返回 {}（由调用方回落内置规则）。"""
    out = {}
    keys = {"形象": "person", "场景": "scene", "产品": "product", "动作": "action"}
    for raw in str(text or "").splitlines():
        line = raw.strip().lstrip("-*#0123456789.、（( \t").strip()
        for cn, k in keys.items():
            if line.startswith(cn + "：") or line.startswith(cn + ":"):
                v = line[len(cn) + 1:].strip()
                if v:
                    out[k] = v
    return out


def _project_role_refs(pid: str, role: str, limit: int = 3) -> list:
    """本项目里**指定角色**的参考图（提取提示词专用图位）：
    role ∈ {model, scene, product}，type=ref 且 ref_role=role。"""
    out = []
    for a in _load("assets.json", []):
        if (a.get("project_id") == pid and a.get("type") == "ref"
                and a.get("ref_role") == role and a.get("file")):
            p = FILES / a["file"]
            if p.exists():
                out.append(str(p))
    return out[:limit]


def _vlm_cfg() -> dict:
    """后台里那条**视觉理解**模型的配置（kind=vlm）—— 开关与模型 id 都从后台读，
    这样后台的启用/停用对「提示词识别」是**真生效**的，不是摆设。"""
    models = admin_get().get("models", [])
    return next((m for m in models if m.get("kind") == "vlm"), {}) or {}


def extract_prompts(body: dict) -> dict:
    """提取提示词：**按区块各提各的**（field 指定），读对应图位的参考图 → 视觉模型反推**单段**。

    field ∈ {person, scene, product, action}，各读各的图位：
        person  → role=model   （模特参考图）
        scene   → role=scene   （场景参考图）
        product → role=product （上架产品图）
        action  → role=model   （动作构图也从模特参考图反推：姿势/机位/景别）

    🔴 真平台这一步就是视觉模型干的（读图 → 出文字），本复现版如实照做：
        · 有对应参考图 且 后台视觉模型启用 且 key 可用 → 真调视觉模型
        · 否则 → 回落到内置规则提示词，并标注 source="rule"（界面显示「内置规则」）
    返回 {ok, field, prompt, source ∈ {vlm, rule}, model_name, ref_count}。"""
    pid = str(body.get("project_id", ""))
    field = str(body.get("field", "")).strip() or ""

    ROLE = {"person": "model", "scene": "scene", "product": "product", "action": "model"}
    INST = {"person": EXTRACT_INST_PERSON, "scene": EXTRACT_INST_SCENE,
            "product": EXTRACT_INST_PRODUCT, "action": EXTRACT_INST_ACTION}
    DFLT = {"person": EXTRACT_PERSON, "scene": EXTRACT_SCENE, "product": EXTRACT_PRODUCT,
            "action": EXTRACT_ACTION}

    if field not in ROLE:
        return {"ok": False, "error": f"未知的提取区块：{field or '（空）'}"}

    refs = _project_role_refs(pid, ROLE[field])
    cfg = _vlm_cfg()
    vlm_on = bool(cfg.get("enabled", True))
    vlm_id = str(cfg.get("call_model") or "")

    res = {"ok": True, "field": field, "prompt": DFLT[field],
           "source": "rule", "model_name": "内置规则", "ref_count": len(refs)}
    if refs and vlm_on:
        r = gen.gen_vlm_extract(refs, INST[field], model=vlm_id)
        txt = (r.get("text") or "").strip() if r.get("ok") else ""
        if txt:
            res.update({"prompt": txt, "source": "vlm",
                        "model_name": r.get("model", "") or vlm_id,
                        "usage": r.get("usage", {})})
        else:
            res["vlm_error"] = r.get("error", "返回为空")
    elif not refs:
        res["vlm_error"] = ("动作从模特参考图反推，请先上传模特参考图"
                            if field == "action" else "该区块还没有上传对应的参考图")
    else:
        res["vlm_error"] = "后台已停用视觉理解模型"
    res["demo_rule"] = res["source"] == "rule"
    log_op("extract", pid, body.get("employee_id", ""),
           prompt=f"{field}: {res['prompt'][:40]}",
           model=res["model_name"], gen_count=1)
    return res


def gen_base(body: dict) -> dict:
    """生成支线基础图：kind=person（模特基础照）/ scene（初版场景）。"""
    pid = str(body.get("project_id", ""))
    kind = str(body.get("kind", "person"))
    prompt = str(body.get("prompt", "")).strip()
    refs = [str(p) for p in (body.get("refs") or []) if Path(str(p)).exists()]
    endpoint = str(body.get("endpoint", "seedream"))
    size = str(body.get("size", "")).strip() or "2K"
    small = "scene" if kind == "scene" else "person"
    atype = "base_scene" if kind == "scene" else "base_person"
    r = _run_image(prompt, refs, f"{pid}_{small}_{int(time.time() * 1000)}",
                   size=size, endpoint=endpoint, kind=atype)
    fname = Path(r["paths"][0]).name if r.get("paths") else ""
    a = _add_asset(pid, atype, fname, prompt_used=prompt, model_name=r.get("model", ""),
                   gen_seconds=r.get("secs", 0), qc_status="na")
    log_op("gen_base", pid, body.get("employee_id", ""), prompt=prompt,
           model=r.get("model", ""), gen_count=1, img_count=1 if fname else 0)
    add_round(pid, body.get("employee_id", ""), small, prompt, r.get("model", ""),
              [a["asset_id"]])
    return {"ok": bool(r.get("ok")), "asset": a, "image": fname, "mock": bool(r.get("mock")),
            "secs": r.get("secs", 0), "error": r.get("error", "")}


# 视觉模板的默认「镜头组」：**同一形象 × 同一场景 × 不同动作 / 沟通图**（≈15 张）
# 🔴 这就是「视觉模板」的图 —— 做完它，才拿去和产品图批量组合，而不是"1 形象 + 1 场景"
DEFAULT_SHOTS = [
    ("正面直立 A-pose", "全身正面站姿，双臂自然下垂，目视镜头，表情自然"),
    ("侧身 45°", "身体侧转 45 度，展示侧面版型与轮廓"),
    ("背面全身", "背向镜头，展示背面版型与走线"),
    ("行走抓拍", "自然行走中的抓拍瞬间，动作舒展"),
    ("坐姿", "自然坐姿，上身挺直，画面松弛"),
    ("半身特写", "半身近景特写，突出上半身与面部"),
    ("细节·面料", "面料与工艺细节特写，质感清晰可辨"),
    ("细节·领口肩线", "领口与肩线细节特写"),
    ("景别·远景", "全身远景，人物在场景中占比更小，留出环境"),
    ("景别·近景", "近景构图，人物为画面主体"),
    ("俯拍", "轻微俯视角度，展示整体搭配"),
    ("仰拍", "轻微仰视角度，拉长身形比例"),
    ("侧后方视角", "从侧后方拍摄，展示侧背身型"),
    ("抬手动作", "单臂自然抬起，展示腋下与侧摆的版型"),
    ("转身动作", "转身瞬间的动态，衣摆自然摆动"),
]


def gen_shots(body: dict) -> dict:
    """生成**镜头组** —— 视觉模板的那组图（≈15 张）。

    一个形象 × 一个场景 × N 个动作/沟通图 → N 张「模板图」。
    🔑 这一步产出的**不是成品**：产品图要到「批量生成」那一步才进来。
    """
    pid = str(body.get("project_id", ""))
    person = str(body.get("person_prompt", "")).strip()
    scene = str(body.get("scene_prompt", "")).strip()
    acts = body.get("actions") or [a for a, _ in DEFAULT_SHOTS]
    detail = {a: d for a, d in DEFAULT_SHOTS}
    ar = _load("assets.json", [])

    def _f(aid):
        for x in ar:
            if x["asset_id"] == aid and x.get("file") and (FILES / x["file"]).exists():
                return str(FILES / x["file"])
        return ""

    refs = [p for p in (_f(str(body.get("person_asset_id", ""))),
                        _f(str(body.get("scene_asset_id", "")))) if p]
    out, model = [], ""
    for i, act in enumerate(acts):
        # 镜头 = 形象 + 场景 + 动作构图（**不含产品**：产品图要到「批量生成」才进来）
        prompt = compose_prompt(person, scene, "", str(act))
        r = _run_image(prompt, refs, f"{pid}_shot_{int(time.time() * 1000)}_{i}", kind="shot")
        model = r.get("model", "") or model
        fname = Path(r["paths"][0]).name if r.get("paths") else ""
        a = _add_asset(pid, "shot", fname, prompt_used=prompt, model_name=r.get("model", ""),
                       gen_seconds=r.get("secs", 0), qc_status="na",
                       shot_index=i + 1, action_prompt=str(act))
        out.append({"shot_index": i + 1, "action": str(act),
                    "detail": detail.get(str(act), ""), "asset": a})
    log_op("gen_shots", pid, body.get("employee_id", ""),
           prompt=f"镜头组 · {len(out)} 个镜头（{person[:16]}…）", model=model,
           gen_count=len(out), img_count=len(out))
    add_round(pid, body.get("employee_id", ""), "shot", f"镜头组 × {len(out)}", model,
              [s["asset"]["asset_id"] for s in out])
    return {"ok": True, "shots": out, "count": len(out), "mock": True}


def compose(body: dict) -> dict:
    """组合生成：款（项目参考图）× 形象（base_person）× 场景（base_scene）→ 成品。

    · 不带 recipe_id → 全新一轮（成品 recipe_id 为空，等保存入库时才生成新配方）
    · 带 recipe_id   → **复用这份配方**：新成品挂到同一配方 id 下
      → 配方因此能**累积多批产出**（跨不同款），这是素材库"看它出过的所有图"的前提
    """
    pid = str(body.get("project_id", ""))
    sku = str(body.get("sku", ""))
    rid = str(body.get("recipe_id", ""))
    # 🔴 组合提示词 = 形象 + 场景 + 产品 + 动作构图（真拼；四段各自可来自模板 / 本行填写）
    person_p = str(body.get("person_prompt", "")).strip()
    scene_p = str(body.get("scene_prompt", "")).strip()
    product_p = str(body.get("product_prompt", "")).strip()
    action_p = str(body.get("action_prompt", "")).strip()
    built = compose_prompt(person_p, scene_p, product_p, action_p)
    prompt = built or str(body.get("prompt", "")).strip() or "生成一张商品展示成品图"
    ar = _load("assets.json", [])
    refs = []

    def _f(aid):
        for a in ar:
            if a["asset_id"] == aid and a.get("file"):
                p = FILES / a["file"]
                if p.exists():
                    return str(p)
        return ""

    pimg, simg = _f(str(body.get("person_asset_id", ""))), _f(str(body.get("scene_asset_id", "")))
    if pimg:
        refs.append(pimg)
    if simg:
        refs.append(simg)
    # 项目内的参考图（＝要上架的款）也一起送进去
    for a in ar:
        if a.get("project_id") == pid and a.get("type") == "ref" and a.get("file"):
            p = FILES / a["file"]
            if p.exists():
                refs.append(str(p))
    refs = refs[:4]
    size = str(body.get("size", "")).strip() or "2K"
    r = _run_image(prompt, refs, f"{pid}_final_{int(time.time() * 1000)}",
                   size=size, kind="final")
    fname = Path(r["paths"][0]).name if r.get("paths") else ""
    a = _add_asset(pid, "final", fname, sku=sku, prompt_used=prompt, recipe_id=rid,
                   model_name=r.get("model", ""), gen_seconds=r.get("secs", 0),
                   # 🔴 联调模式也算**有效产出**（有占位图，走同一条落盘/登记路径）→ 必须是待标记；
                   #    否则 mock 演示时批量跑完的图在批量页验收列会显示成「—」
                   qc_status="pending" if (r.get("ok") or r.get("mock")) else "na",
                   saved=bool(rid),   # 复用已有配方 → 一出生就属于这份配方，直接算已入库
                   # 记下这一张成品的**四段来源**，界面上要能拆开展示「组合词由哪四段拼的」
                   person_prompt=person_p, scene_prompt=scene_p, product_prompt=product_p,
                   action_prompt=action_p)
    log_op("compose", pid, body.get("employee_id", ""), prompt=prompt,
           model=r.get("model", ""), gen_count=1, img_count=1 if fname else 0,
           extra=({"recipe_id": rid, "mode": "reuse"} if rid else {}))
    add_round(pid, body.get("employee_id", ""), "final", prompt, r.get("model", ""),
              [a["asset_id"]], extra={"sku": sku, "recipe_id": rid,
                                      "product_prompt": product_p,
                                      "mode": "reuse" if rid else "new"})
    return {"ok": bool(r.get("ok")), "asset": a, "image": fname, "mock": bool(r.get("mock")),
            "secs": r.get("secs", 0), "error": r.get("error", "")}


def gen_plain(body: dict) -> dict:
    """自由出图（文生图 / 图生图）—— 单纯的图片编辑，不走配方链路。

    · 不带 refs → **纯文生图**（只吃文字）
    · 带 refs   → **图生图**（图片素材 + 文字，编辑这几张图）
    产出登记为 assets(type=free_out)，与「参考图 / 支线 / 成品」完全隔离。
    """
    pid = str(body.get("project_id", ""))
    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        return {"ok": False, "error": "请先输入提示词"}
    size = str(body.get("size", "")).strip() or "2K"
    endpoint = str(body.get("endpoint", "seedream"))
    ar = _load("assets.json", [])
    refs = []
    for aid in [str(x) for x in (body.get("refs") or [])]:
        a = next((x for x in ar if x["asset_id"] == aid and x.get("file")), None)
        if a:
            p = FILES / a["file"]
            if p.exists():
                refs.append(str(p))
    r = _run_image(prompt, refs, f"{pid}_free_{int(time.time() * 1000)}",
                   size=size, endpoint=endpoint, kind="free_out")
    fname = Path(r["paths"][0]).name if r.get("paths") else ""
    a = _add_asset(pid, "free_out", fname, prompt_used=prompt, model_name=r.get("model", ""),
                   gen_seconds=r.get("secs", 0), qc_status="na")
    mode = "img2img" if refs else "txt2img"
    log_op("gen_plain", pid, body.get("employee_id", ""), prompt=prompt,
           model=r.get("model", ""), gen_count=1, img_count=1 if fname else 0,
           extra={"mode": mode, "size": size})
    add_round(pid, body.get("employee_id", ""), "free", prompt, r.get("model", ""),
              [a["asset_id"]], extra={"mode": mode, "size": size})
    return {"ok": bool(r.get("ok")), "asset": a, "image": fname, "mock": bool(r.get("mock")),
            "secs": r.get("secs", 0), "mode": mode, "size": size, "error": r.get("error", "")}


def upload_ref(body: dict) -> dict:
    """上传参考图 → data/files/，登记为 assets（type=ref，或 purpose=free 时 type=free_in）。"""
    pid = str(body.get("project_id", ""))
    data = str(body.get("data", ""))
    name = str(body.get("name", "") or "ref")
    if not data:
        return {"ok": False, "error": "空数据"}
    try:
        raw = base64.b64decode(data)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "base64 解析失败"}
    if len(raw) < 100:
        return {"ok": False, "error": "文件过小"}
    safe = re.sub(r"[^A-Za-z0-9_.]", "_", name)[:40]
    ext = Path(safe).suffix.lower()
    if ext not in IMAGE_EXT:
        ext = ".jpg"
    FILES.mkdir(parents=True, exist_ok=True)
    fname = f"ref_{int(time.time() * 1000)}{ext}"
    (FILES / fname).write_bytes(raw)
    # purpose=free → 自由出图的输入图（type=free_in），不进项目参考图区
    atype = "free_in" if str(body.get("purpose", "")) == "free" else "ref"
    # role ∈ {model, scene, product}：三个「提取提示词」专属图位（其余为普通项目素材，ref_role=""）
    role = str(body.get("role", "")).strip() or ""
    a = _add_asset(pid, atype, fname, prompt_used="", ref_role=role)
    return {"ok": True, "asset": a}


# ---------------- 后台管理（模型 · 节点 · 成员 · 全局配置） ----------------
# 🔴 口径（与「派生统计不落盘」一致）：
#   · **配置**落盘 —— 模型单价 / 节点开关与优先级 / 成员配额 / 全局参数
#   · **统计**现算 —— 用量与成本从 assets 现算，绝不写第二份真相
#   · 唯一例外：成员 `used` 模拟「从计费系统同步来的月度用量」（外部数据，
#     与 performance.json 同性质），界面上明示来源
# 🔑 这一节对标同类项目（神笔马良 Soul Canvas `src/lib/api-node-manager.ts`）里的
#    **节点管理**：节点测速 / 选择策略 / 故障转移 / 健康缓存 —— 它只做在代码里、
#    靠改 .env 重启；这里把它**做成看得见的后台**。

ADMIN_MODELS_DEF = [
    {"model_id": "m-vlm", "name": "Doubao Seed 2.0 Pro", "vendor": "字节跳动",
     "via": "artsapi 网关", "match": ["doubao-seed-2-0-pro"], "unit_price": 0.0,
     "priority": 1, "enabled": True, "kind": "vlm",
     "call_model": "doubao-seed-2-0-pro-260215",
     "purpose": "参考图 → 反推提示词（提示词识别）",
     "note": "已验证可读图；按 token 计费，不计入按张成本"},
    {"model_id": "m-seedream", "name": "Seedream 5.0 Pro", "vendor": "字节跳动",
     "via": "artsapi 网关", "match": ["seedream"], "unit_price": 0.60, "priority": 1,
     "enabled": True, "purpose": "主链路默认：模特 / 场景 / 镜头组 / 成品",
     "note": "细节与材质还原最稳；>2.36MP 计 0.60，≤2.36MP 计 0.30"},
    {"model_id": "m-gemini", "name": "Gemini 2.5 Flash Image", "vendor": "Google",
     "via": "Google AI Studio", "match": ["gemini"], "unit_price": 0.30, "priority": 2,
     "enabled": True, "purpose": "可选备选：人物一致性（自由出图下拉里选）",
     "note": "换装换动作时人物一致性最好，单价最低；需 GEMINI_API_KEY，未配"},
    {"model_id": "m-gptimage", "name": "GPT Image 1", "vendor": "OpenAI",
     "via": "OpenAI Platform", "match": ["gpt-image"], "unit_price": 0.50, "priority": 3,
     "enabled": True, "purpose": "可选备选：成品 / 成套组合",
     "note": "构图与指令遵循强；需 OPENAI_API_KEY，未配"},
    {"model_id": "m-flux", "name": "FLUX.2", "vendor": "Black Forest Labs",
     "via": "fal.ai", "match": ["flux"], "unit_price": 0.40, "priority": 4,
     "enabled": False, "purpose": "规划中：场景 / 自由出图",
     "note": "氛围与光影强；fal.ai 端点尚未接入，当前停用"},
    {"model_id": "m-rmbg", "name": "RMBG-2.0", "vendor": "BRIA",
     "via": "本地部署", "match": ["rmbg", "bria"], "unit_price": 0.02, "priority": 5,
     "enabled": True, "purpose": "抠图 / 去背",
     "note": "本地跑，只计电费；不算进生图预算"},
]

ADMIN_NODES_DEF = [
    {"node_id": "n-artsapi", "name": "artsapi 兼容网关",
     "base_url": "https://ai.artsapi.com/v1",
     "models": ["Seedream 5.0 Pro", "Doubao Seed 2.0 Pro"],
     "priority": 1, "enabled": True, "mode": "async",
     "health": {"status": "ok", "latency_ms": 645, "success_rate": 0.997,
                "probes": 183, "failovers": 0, "last_check": ""}},
    {"node_id": "n-ark", "name": "火山方舟 ARK（直连）",
     "base_url": "https://ark.cn-beijing.volces.com/api/v3",
     "models": ["Seedream 5.0 Pro"],
     "priority": 2, "enabled": False, "mode": "async",
     "health": {"status": "off", "latency_ms": 760, "success_rate": 0.998,
                "probes": 214, "failovers": 0, "last_check": ""}},
    {"node_id": "n-google", "name": "Google AI Studio",
     "base_url": "https://generativelanguage.googleapis.com/v1beta",
     "models": ["Gemini 2.5 Flash Image"], "priority": 3, "enabled": False, "mode": "sync",
     "health": {"status": "off", "latency_ms": 430, "success_rate": 0.994,
                "probes": 268, "failovers": 0, "last_check": ""}},
    {"node_id": "n-openai", "name": "OpenAI Platform",
     "base_url": "https://api.openai.com/v1",
     "models": ["GPT Image 1"], "priority": 4, "enabled": False, "mode": "sync",
     "health": {"status": "off", "latency_ms": 1620, "success_rate": 0.961,
                "probes": 96, "failovers": 2, "last_check": ""}},
    {"node_id": "n-fal", "name": "fal.ai",
     "base_url": "https://fal.run",
     "models": ["FLUX.2"], "priority": 5, "enabled": False, "mode": "async",
     "health": {"status": "off", "latency_ms": 0, "success_rate": 0.0,
                "probes": 0, "failovers": 0, "last_check": ""}},
]

ADMIN_MEMBERS_DEF = [
    {"member_id": "u-01", "employee_id": "lin.design", "name": "林 · 视觉设计",
     "role": "设计负责人", "quota": 4000, "used": 1842, "status": "active",
     "models": ["Seedream 5.0 Pro", "Gemini 2.5 Flash Image", "GPT Image 1"]},
    {"member_id": "u-02", "employee_id": "chen.design", "name": "陈 · 主设（内衣线）",
     "role": "设计师", "quota": 3000, "used": 2410, "status": "active",
     "models": ["Seedream 5.0 Pro", "Gemini 2.5 Flash Image"]},
    {"member_id": "u-03", "employee_id": "wu.design", "name": "吴 · 主设（家居线）",
     "role": "设计师", "quota": 3000, "used": 1176, "status": "active",
     "models": ["Seedream 5.0 Pro", "Gemini 2.5 Flash Image"]},
    {"member_id": "u-04", "employee_id": "zhao.visual", "name": "赵 · 视觉策划",
     "role": "策划（只读）", "quota": 800, "used": 214, "status": "active",
     "models": ["Seedream 5.0 Pro"]},
    {"member_id": "u-05", "employee_id": "sun.intern", "name": "孙 · 设计实习",
     "role": "设计师（受限）", "quota": 600, "used": 588, "status": "limited",
     "models": ["Gemini 2.5 Flash Image"]},
    {"member_id": "u-06", "employee_id": "huang.design", "name": "黄 · 主设（大杯线）",
     "role": "设计师", "quota": 3000, "used": 0, "status": "paused",
     "models": ["Seedream 5.0 Pro"]},
]

ADMIN_CONFIG_DEF = {
    "default_shots": 15,          # 一份视觉模板默认几个镜头
    "per_shot_variants": 2,       # 每个镜头出几张挑一张
    "max_concurrency": 4,         # 批量最大并发
    "retry": 2,                   # 单行失败重试次数
    "retry_queue": True,          # 失败行自动回队
    "batch_review": True,         # 批量结果需人工在批量页逐行「标记合格 / 需重做」后才算确认
    "node_strategy": "priority",  # priority / latency / round-robin
    "storage": "data/files",
    "share_default": "team",      # 新建配方的默认可见性
}

_NODE_STRATEGY_CN = {"priority": "按优先级", "latency": "按最低延迟", "round-robin": "轮询"}


def admin_get() -> dict:
    """读后台配置；首次访问时用默认值落盘（惰性 seed）。

    🔴 之后代码里新增的默认条目会**自动补缺**（只补没有的，不覆盖已有字段）——
       否则老的 admin.json 永远看不到新模型，后台就和代码脱节了。"""
    d = _load("admin.json", None)
    if not isinstance(d, dict) or not d.get("models"):
        d = {"models": json.loads(json.dumps(ADMIN_MODELS_DEF)),
             "nodes": json.loads(json.dumps(ADMIN_NODES_DEF)),
             "members": json.loads(json.dumps(ADMIN_MEMBERS_DEF)),
             "config": json.loads(json.dumps(ADMIN_CONFIG_DEF)),
             "updated_at": _now(), "updated_by": "system"}
        _save("admin.json", d)
        return d
    # —— 补缺：默认里有、现有文件里没有的条目（补缺不算用户变更，不动 updated_at）——
    changed = False
    d.setdefault("models", [])
    have_m = {m.get("model_id") for m in d["models"]}
    for m in ADMIN_MODELS_DEF:
        if m["model_id"] not in have_m:
            d["models"].append(json.loads(json.dumps(m)))
            changed = True
    d.setdefault("nodes", [])
    have_n = {n.get("node_id") for n in d["nodes"]}
    for n in ADMIN_NODES_DEF:
        if n["node_id"] not in have_n:
            d["nodes"].append(json.loads(json.dumps(n)))
            changed = True
    for node in d["nodes"]:          # 节点上挂的模型名也补缺
        tpl = next((n for n in ADMIN_NODES_DEF if n["node_id"] == node.get("node_id")), None)
        if not tpl:
            continue
        for mn in tpl.get("models", []):
            if mn not in (node.get("models") or []):
                node.setdefault("models", []).append(mn)
                changed = True
    d.setdefault("config", {})
    for k, v in ADMIN_CONFIG_DEF.items():
        if k not in d["config"]:
            d["config"][k] = v
            changed = True
    if changed:
        _save("admin.json", d)
    return d


def _unit_price(model_name: str, models: list) -> float:
    """按模型名关键词匹配单价（配置里 `match` 字段）。匹配不到＝0（不编价）。"""
    m = str(model_name or "").lower()
    for cfg in models:
        for kw in (cfg.get("match") or []):
            if kw and str(kw).lower() in m:
                return float(cfg.get("unit_price") or 0)
    return 0.0


def admin_usage() -> dict:
    """🔴 **用量与成本一律现算**（从 assets 现算，不落盘）——避免两份真相。"""
    models = admin_get().get("models", [])
    assets = _load("assets.json", [])
    by_model, by_project = {}, {}
    total_imgs, total_cost = 0, 0.0
    for a in assets:
        if a.get("type") in ("ref", "free_in"):   # 导入类素材不是"生成出来的"，不计成本
            continue
        mn = str(a.get("model_name") or "")
        if not mn:
            continue
        p = _unit_price(mn, models)
        k = by_model.setdefault(mn, {"model": mn, "count": 0, "cost": 0.0})
        k["count"] += 1
        k["cost"] += p
        pid = a.get("project_id") or "-"
        j = by_project.setdefault(pid, {"project_id": pid, "count": 0, "cost": 0.0})
        j["count"] += 1
        j["cost"] += p
        total_imgs += 1
        total_cost += p
    rows = sorted(by_model.values(), key=lambda x: -x["cost"])
    for r in rows:
        r["cost"] = round(r["cost"], 2)
        r["unit_price"] = _unit_price(r["model"], models)
        r["pct"] = round(r["cost"] / total_cost * 100, 1) if total_cost else 0
    pname = {p["project_id"]: p.get("name", "") for p in list_projects()}
    prows = sorted(by_project.values(), key=lambda x: -x["cost"])
    for r in prows:
        r["cost"] = round(r["cost"], 2)
        r["name"] = pname.get(r["project_id"], r["project_id"])
        r["pct"] = round(r["cost"] / total_cost * 100, 1) if total_cost else 0
    # 批量成功率（任务队列的客观口径：已完成 / 已结束）
    bt = _load("batch.json", {})
    done = fail = wait = 0
    for _, rows_ in bt.items():
        for r in rows_:
            s = r.get("status")
            if s == "done":
                done += 1
            elif s == "fail":
                fail += 1
            elif s == "wait":
                wait += 1
    ended = done + fail
    return {"images": total_imgs, "cost": round(total_cost, 2),
            "avg": round(total_cost / total_imgs, 3) if total_imgs else 0,
            "by_model": rows, "by_project": prows,
            "batch": {"done": done, "fail": fail, "wait": wait,
                      "success_rate": round(done / ended * 100, 1) if ended else 0},
            "recipes": len(_load("recipes.json", [])),
            "shots": sum(1 for a in assets if a.get("type") == "shot"),
            "finals": sum(1 for a in assets if a.get("type") == "final")}


def admin_state() -> dict:
    """后台一页所需的全部数据（配置 ＋ 现算统计）。"""
    d = admin_get()
    return {"ok": True, "models": d.get("models", []), "nodes": d.get("nodes", []),
            "members": d.get("members", []), "config": d.get("config", {}),
            "usage": admin_usage(), "updated_at": d.get("updated_at", ""),
            "updated_by": d.get("updated_by", ""),
            "strategy_cn": _NODE_STRATEGY_CN.get((d.get("config") or {}).get("node_strategy", ""), "—")}


def admin_save(body: dict) -> dict:
    """保存某一分区（models / nodes / members / config）。"""
    sec = str(body.get("section", ""))
    if sec not in ("models", "nodes", "members", "config"):
        return {"ok": False, "error": "未知分区"}
    d = admin_get()
    d[sec] = body.get("data")
    d["updated_at"] = _now()
    d["updated_by"] = str(body.get("employee_id", "")).strip() or "unknown"
    _save("admin.json", d)
    _sec_cn = {"models": "模型清单", "nodes": "节点与调度", "members": "成员与配额", "config": "全局配置"}
    log_op("admin_save", "", d["updated_by"], prompt=f"后台配置变更 · {_sec_cn.get(sec, sec)}")
    return {"ok": True, "updated_at": d["updated_at"]}


def admin_probe(body: dict) -> dict:
    """节点测速 —— 联调态下为**模拟探针**（不真发请求），只演示"看得见健康度"这件事。"""
    d = admin_get()
    nodes = d.get("nodes", [])
    tick = int(time.time() * 1000)
    for i, n in enumerate(nodes):
        if not n.get("enabled"):
            n["health"] = {"status": "off", "latency_ms": 0, "success_rate": 0.0,
                           "probes": 0, "failovers": n.get("health", {}).get("failovers", 0),
                           "last_check": _now()}
            continue
        seed = tick + i * 977
        base = 380 if "google" in n.get("node_id", "") else (700 if "ark" in n.get("node_id", "") else 1500)
        lat = base + (seed % 260)
        h = n.get("health", {})
        h["latency_ms"] = lat
        h["probes"] = int(h.get("probes", 0)) + 1
        h["success_rate"] = round(min(0.999, max(0.90, float(h.get("success_rate") or 0.95)
                                                 + ((seed % 7) - 3) / 1000)), 3)
        h["status"] = "ok" if h["success_rate"] >= 0.99 else "warn"
        h["last_check"] = _now()
        n["health"] = h
    d["nodes"] = nodes
    d["updated_at"] = _now()
    _save("admin.json", d)
    log_op("admin_probe", "", str(body.get("employee_id", "")), prompt="后台 · 节点测速", gen_count=len(nodes))
    return {"ok": True, "nodes": nodes, "checked_at": d["updated_at"]}


def mode_get() -> dict:
    """当前模式 + 各端点 key 状态（前端顶部开关据此渲染）。"""
    return {"ok": True, "mock_mode": MOCK_MODE, "keys": key_status()}


def mode_set(body: dict) -> dict:
    """切换模式的路由入口：body = {"mock": true|false}。"""
    return set_mock(bool(body.get("mock")))


# ---------------- 种子数据（首次启动给个能演的初始态） ----------------
def seed_if_empty() -> None:
    if list_projects():
        return
    p = create_project({"name": "24AW 秋冬主推", "employee_id": "lin.design"})["project"]
    src = PHOTOS / "DEMO-01"
    if src.exists():
        for f in sorted(src.iterdir()):
            if f.suffix.lower() in IMAGE_EXT:
                fname = f"ref_seed_{int(time.time() * 1000)}{f.suffix.lower()}"
                shutil.copy2(f, FILES / fname)
                _add_asset(p["project_id"], "ref", fname)
                time.sleep(0.001)
    print(f"[seed] 已创建演示项目 {p['name']}（{p['project_id']}）")


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        def _np(o):
            try:
                import numpy as np
                if isinstance(o, np.bool_):
                    return bool(o)
                if isinstance(o, np.integer):
                    return int(o)
                if isinstance(o, np.floating):
                    return float(o)
                if isinstance(o, np.ndarray):
                    return o.tolist()
            except ImportError:
                pass
            raise TypeError(str(type(o)))
        data = json.dumps(obj, ensure_ascii=False, default=_np).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path: Path):
        if not path.exists() or not path.is_file():
            self._json({"error": "not found"}, 404)
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p, q = u.path, urllib.parse.parse_qs(u.query)
        try:
            if p in ("/", "/index.html"):
                self._file(INDEX)
            elif p == "/api/projects":
                self._json(list_projects())
            elif p == "/api/state":
                pid = q.get("project_id", [""])[0]
                # 🔴 轮次**必须一次给全**：前端还要在本地按 kind 过滤（右侧栏只留
                #    模特/场景/成品，不含镜头组）→ 若后端先按 80 截断，前端再筛一遍
                #    就会**静默吃掉最早的若干条**（实测 81 条只显示 70 条）。
                self._json({"projects": list_projects(), "assets": list_assets(pid),
                            "recipes": recipes_with_stats(pid), "performance": list_perf(),
                            "rounds": list_rounds(pid, limit=400),
                            "free_inputs": list_free_inputs(pid),
                            "batch_rows": get_batch_rows(pid),
                            "mock_mode": MOCK_MODE, "keys": key_status()})
            elif p == "/api/rounds":
                self._json(list_rounds(q.get("project_id", [""])[0],
                                       q.get("kind", [""])[0],
                                       int(q.get("limit", ["80"])[0])))
            elif p == "/api/assets":
                self._json(list_assets(q.get("project_id", [""])[0]))
            elif p == "/api/recipes":
                self._json(recipes_with_stats(q.get("project_id", [""])[0]))
            elif p == "/api/recipe_detail":
                self._json(get_recipe_detail(q.get("recipe_id", [""])[0]))
            elif p == "/api/performance":
                self._json(list_perf())
            elif p == "/api/logs":
                self._json(list_logs(int(q.get("limit", ["200"])[0])))
            elif p == "/api/admin_state":
                self._json(admin_state())
            elif p.startswith("/files/"):
                target = (FILES / p[len("/files/"):]).resolve()
                if FILES.resolve() not in target.parents and target != FILES.resolve():
                    self._json({"error": "forbidden"}, 403)
                    return
                self._file(target)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            self._json({"ok": False, "error": "请求解析失败"}, 400)
            return
        routes = {
            "/api/project_create": create_project,
            "/api/upload_ref": upload_ref,
            "/api/extract": extract_prompts,
            "/api/gen_base": gen_base,
            "/api/gen_shots": gen_shots,
            "/api/compose": compose,
            "/api/gen_plain": gen_plain,
            "/api/batch_save": save_batch_rows,
            "/api/asset_mark": mark_asset,
            "/api/asset_delete": delete_asset,
            "/api/recipe_save": save_recipe,
            "/api/perf_import": import_perf,
            "/api/admin_save": admin_save,
            "/api/admin_probe": admin_probe,
            "/api/mode": mode_set,
        }
        fn = routes.get(u.path)
        if not fn:
            self._json({"error": "not found"}, 404)
            return
        try:
            self._json(fn(body))
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "error": str(e)}, 500)


def main():
    ap = argparse.ArgumentParser(description="AIGC 出图工作台 v2")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--mock", action="store_true", help="强制联调模式（不真调生图 API）")
    args = ap.parse_args()
    for d in (DATA, FILES, UPLOADS):
        d.mkdir(parents=True, exist_ok=True)
    if args.mock:
        force_mock()
    seed_if_empty()
    admin_get()          # 保证后台配置（admin.json）存在
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"AIGC 出图工作台已启动:  http://localhost:{args.port}"
          + ("   [联调模式]" if args.mock else ""))
    print("（浏览器打开即用；Ctrl+C 退出）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
