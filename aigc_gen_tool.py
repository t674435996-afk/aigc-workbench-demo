# -*- coding: utf-8 -*-
"""
AIGC 出图工作台 · 生成引擎（静物 / 细节 / 模特）
================================================
素材 → 按图类型预设 → 多模型生图 → 成品目录 + 元数据 CSV。

设计要点：
  · 一个入口收敛多模型：图类型预设决定"这类图调哪个模型、用几张参考图、出几张"
  · 无 API key 自动降级 mock（只产出 prompt 与占位说明），保证整条链路可离线联调
  · 全部依赖标准库；真实调用用 requests

用法:
  python aigc_gen_tool.py --sku DEMO-01 --presets still_life,detail,onfoot [--tags "material=帆布"]

目录约定:
  photos/<sku>/            素材（产品多角度 + 细节）
  templates/               视觉模板（模特 / 场景参考）
  out/<sku>/<preset>/      成品按图类型分目录
  out/<sku>/gen_meta.csv   每张图的元数据（prompt / 模型 / 状态 / 耗时）

环境变量（缺失时进入 mock 模式）:
  IMAGE_API_KEY_A    视觉模型 A（细节 / 静物）
  IMAGE_API_KEY_B    视觉模型 B（模特 / 融合）
  IMAGE_API_KEY_C    视觉模型 C（多图融合）
"""

import argparse
import csv
import json
import os
import shutil
import sys
from pathlib import Path

# ---------- 配置 ----------
BASE = Path(__file__).parent
PRESETS_FILE = BASE / "presets.yaml"
PHOTOS_DIR = BASE / "photos"
OUT_DIR = BASE / "out"
TEMPLATES_DIR = BASE / "templates"      # 视觉模板放这里


def _load_env() -> None:
    """加载 .env（先自己目录，再上一级目录），避免每次手动 export。"""
    for env_file in (BASE / ".env", BASE.parent / ".env"):
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

# ---------- 预设加载（极简 YAML 解析，避免额外依赖） ----------
def load_presets() -> dict:
    """读取图类型预设 YAML（支持键值、'>' 折叠多行字符串、行内注释）。"""
    import re

    raw = PRESETS_FILE.read_text(encoding="utf-8")
    presets, current = {}, None
    in_defaults = False
    multiline_key, multiline_buf = None, []
    for line in raw.splitlines():
        line = re.split(r"\s+#", line, maxsplit=1)[0].rstrip()   # 仅切"空格+#"注释，保护 #F5F3EF
        if not line.strip():
            continue
        if multiline_key is not None:
            if line.startswith(" "):
                multiline_buf.append(line.strip())
                continue
            current[multiline_key] = " ".join(multiline_buf).strip()
            multiline_key, multiline_buf = None, []
        if not line.startswith(" ") and line.rstrip().endswith(":"):
            if current and not in_defaults and current.get("endpoint"):
                presets[current["_name"]] = current          # 只收真正带端点的图类型块
            current = {"_name": line.split(":")[0].strip()}
            in_defaults = current["_name"] == "defaults"
            continue
        if current is None:
            continue
        if ":" in line:
            k, v = line.strip().split(":", 1)
            k, v = k.strip(), v.strip()
            if v == ">":
                multiline_key, multiline_buf = k, []
            elif v:
                current[k] = v.strip('"').strip("'")
    if multiline_key is not None:
        current[multiline_key] = " ".join(multiline_buf).strip()
    if current and not in_defaults and current.get("endpoint"):
        presets[current["_name"]] = current
    return presets


# ---------- 中文路径兼容读取（Windows + opencv 对非 ASCII 文件名不稳） ----------
def _imread_cn(path: str):
    """cv2.imread 在 Windows 上对中文路径不稳定，改用 np.fromfile + imdecode。"""
    try:
        import cv2
        import numpy as np
        data = np.fromfile(path, dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:  # noqa: BLE001
        return None


# ---------- 素材质检（清晰度 / 曝光） ----------
def quality_check(photo_path: str) -> dict:
    """清晰度(Laplacian 方差) / 曝光(直方图)。返回 {ok, sharpness, exposure, reason}"""
    if not os.path.exists(photo_path):
        return {"ok": False, "sharpness": 0.0, "exposure": 0.0, "reason": "file missing"}
    try:
        import cv2
        img = _imread_cn(photo_path)
        if img is None:
            return {"ok": False, "sharpness": 0.0, "exposure": 0.0, "reason": "unreadable"}
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()          # >60 视为清晰
        exposure = float(gray.mean()) / 255.0                       # 0.2~0.85 正常
        ok = (sharpness > 60.0) and (0.2 < exposure < 0.85)
        return {"ok": ok, "sharpness": round(sharpness, 1),
                "exposure": round(exposure, 2), "reason": "" if ok else "blur/exposure"}
    except ImportError:
        return {"ok": True, "sharpness": 0.0, "exposure": 0.0,
                "reason": "opencv not installed, skip"}


# ---------- 成品 QC（模糊 / 几何一致 / 色差，无 opencv 则跳过放行） ----------
def qc_check(image_path: str, ref_path: str = "") -> dict:
    """QC 闸门：模糊(Laplacian) / 几何一致(ORB 特征匹配) / 色差ΔE(整图近似)。
    返回 {ok, sharpness, dE, geo, reason}。无 opencv 时跳过放行（不阻塞流程）。"""
    try:
        import cv2
        import numpy as np
        img = _imread_cn(image_path)
        if img is None:
            return {"ok": False, "sharpness": 0.0, "dE": 99.0, "geo": 0.0, "reason": "unreadable"}
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        dE, geo = 0.0, 0.0
        if ref_path and os.path.exists(ref_path):
            ref = _imread_cn(ref_path)
            if ref is not None:
                ref_gray = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY)
                orb = cv2.ORB_create(1000)
                k1, d1 = orb.detectAndCompute(gray, None)
                k2, d2 = orb.detectAndCompute(ref_gray, None)
                if d1 is not None and d2 is not None and len(d1) > 0 and len(d2) > 0:
                    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
                    m = sorted(bf.match(d1, d2), key=lambda x: x.distance)[:50]
                    if m:
                        good = [x for x in m if x.distance < 50]
                        geo = round(len(good) / len(m), 2)
                ia = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(float)
                ib = cv2.cvtColor(ref, cv2.COLOR_BGR2LAB).astype(float)
                if ia.shape != ib.shape:                     # 尺寸不一致 → 缩放参考图对齐
                    ib = cv2.resize(ib, (ia.shape[1], ia.shape[0]))
                dE = round(float(np.mean(np.linalg.norm(ia - ib, axis=2))), 2)
        ok = (sharpness > 30.0) and (geo > 0.2 or geo == 0.0)
        return {"ok": ok, "sharpness": round(sharpness, 1), "dE": dE, "geo": geo,
                "reason": "" if ok else "blur/geometry"}
    except ImportError:
        return {"ok": True, "sharpness": 0.0, "dE": 0.0, "geo": 0.0,
                "reason": "opencv not installed, skip"}
    except Exception as e:  # noqa: BLE001   单张 QC 异常不崩整批，标记跳过
        return {"ok": True, "sharpness": 0.0, "dE": 0.0, "geo": 0.0,
                "reason": f"qc error: {str(e)[:40]}"}


# ---------- 素材发现 ----------
def find_photos(sku: str) -> list:
    """列出 SKU 素材目录下的图片（按文件名排序）。"""
    d = PHOTOS_DIR / sku
    if not d.exists():
        print(f"[!] 素材目录不存在: {d}（请确认 photos/{sku}/ 下有图）")
        return []
    exts = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
    return [str(p) for p in sorted(d.iterdir()) if p.suffix.lower() in exts]


# ---------- prompt 组装 ----------
def build_prompt(preset: dict, tags: dict) -> str:
    """用标签回填 prompt 占位（{material} 等）。"""
    p = preset.get("prompt", "")
    for k, v in tags.items():
        p = p.replace("{" + k + "}", str(v))
    return p


# ---------- API 调用（真实实现 + mock 降级） ----------
# 多端点：同一入口按图类型分派到不同模型；key 缺失即降级 mock，链路仍可完整走通。
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")       # 端点一：GPT Image 系列
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")       # 端点二：Gemini 2.5 Flash Image
ARK_API_KEY = os.environ.get("ARK_API_KEY", "")             # 端点三：火山方舟官方直连
ARTSAPI_API_KEY = os.environ.get("ARTSAPI_API_KEY", "")     # 端点三备选：兼容网关
SEEDREAM_PROVIDER = os.environ.get("SEEDREAM_PROVIDER", "artsapi").strip().lower()


def _mime(p) -> str:
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "webp": "image/webp"}.get(Path(p).suffix.lower(), "image/jpeg")


def _save_urls(urls: list, out_path: Path) -> list:
    import requests
    saved = []
    for i, u in enumerate(urls):
        if not u:
            continue
        img = requests.get(u, timeout=120).content
        p = out_path.with_name(f"{out_path.stem}_{i + 1}{out_path.suffix}")
        p.write_bytes(img)
        saved.append(str(p))
    return saved


TPL_RULE = ("【重要：参考图的最后一张是视觉模板】请严格保持该模板中的模特、姿势、服装、"
            "场景与构图完全不变，只把前面参考图中的产品穿戴/放置到该模板的模特身上。"
            "严禁重新生成新模特或改变姿势场景。")


def gen_openai(preset: dict, refs: list, prompt: str, out_path: Path, kind: str = "") -> dict:
    """端点一 · GPT Image 系列（参考图编辑）。无 key → mock。"""
    if not OPENAI_API_KEY:
        return mock_result(out_path, prompt, preset.get("model", "gpt-image-1"), kind)
    try:
        import base64
        import requests

        with open(refs[0], "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        resp = requests.post(
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": preset.get("model", "gpt-image-1"),
                "prompt": prompt,
                "image": b64,
                "n": int(preset.get("output", 1)),
            },
            timeout=180,
        )
        data = resp.json()
        saved = _save_urls([i.get("url", "") for i in data.get("data", [])], out_path)
        return {"ok": bool(saved), "paths": saved, "model": preset.get("model")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "paths": [], "model": preset.get("model"), "error": str(e)}


def gen_seedream(preset: dict, refs: list, prompt: str, out_path: Path,
                 template: str = "", kind: str = "") -> dict:
    """端点二 · Seedream 5.0 Pro（多图融合）。
    双端点：SEEDREAM_PROVIDER=ark（火山方舟官方直连）/ artsapi（兼容网关）。
    template 非空时作为【最后一张参考图】传入，并自动追加"模板约束"指令。无 key → mock。"""
    if template and os.path.exists(template) and TPL_RULE not in prompt:
        prompt = prompt + " " + TPL_RULE
    # 🔑 选 provider：按 SEEDREAM_PROVIDER 优先，但该端点的 key 缺失时自动回退到另一个有 key 的端点。
    #    避免「key 明明配了、只是 provider 没写对 → 白白走联调」的坑（提取提示词那边本来就有 ark 备选回退，
    #    生图这里不补上，就会两头表现不一致：提取能成、生成却报无 key）。
    is_ark = (SEEDREAM_PROVIDER == "ark")
    api_key = ARK_API_KEY if is_ark else ARTSAPI_API_KEY
    base_url = ("https://ark.cn-beijing.volces.com/api/v3/images/generations" if is_ark
                else "https://ai.artsapi.com/v1/images/generations")
    provider = "ark" if is_ark else "artsapi"
    if not api_key:
        alt_key = ARTSAPI_API_KEY if is_ark else ARK_API_KEY
        if alt_key:
            is_ark = not is_ark
            api_key = alt_key
            base_url = ("https://ark.cn-beijing.volces.com/api/v3/images/generations" if is_ark
                        else "https://ai.artsapi.com/v1/images/generations")
            provider = "ark" if is_ark else "artsapi"
        else:
            # 模型列只留真实模型名：联调状态由界面顶部徽标统一说明，不塞进模型名
            return mock_result(out_path, prompt, preset.get("model", "doubao-seedream-5-0-pro-260628"), kind)
    try:
        import base64
        import requests

        ref_imgs = []
        for r in refs[: int(preset.get("ref_images", 3))]:
            with open(r, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            ref_imgs.append(f"data:{_mime(r)};base64,{b64}")
        if template and os.path.exists(template):
            with open(template, "rb") as f:
                t64 = base64.b64encode(f.read()).decode()
            ref_imgs.append(f"data:{_mime(template)};base64,{t64}")

        body = {
            "model": preset.get("model", "doubao-seedream-5-0-pro-260628"),
            "prompt": prompt,
            "size": preset.get("size", "2K"),
            "response_format": "url",
        }
        if ref_imgs:                        # 无参考图 → 纯文生图（模特基础照 / 场景支线走这条）
            body["image"] = ref_imgs if len(ref_imgs) > 1 else ref_imgs[0]
        if is_ark:                          # 官方直连附加参数（兼容网关不接受）
            body["watermark"] = False
            body["stream"] = False
        resp = requests.post(
            base_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=(20, 180),
        )
        data = resp.json()
        saved = _save_urls([i.get("url", "") for i in data.get("data", [])], out_path)
        if not saved:
            return {"ok": False, "paths": [], "model": preset.get("model"),
                    "error": f"API 无图片返回: {resp.text[:200]}"}
        return {"ok": bool(saved), "paths": saved, "model": preset.get("model"),
                "provider": provider, "usage": data.get("usage") or {}}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "paths": [], "model": preset.get("model"), "error": str(e)}


def gen_gemini(preset: dict, refs: list, prompt: str, out_path: Path,
               template: str = "", kind: str = "") -> dict:
    """端点三 · Gemini 2.5 Flash Image（模特 / 人物融合）。无 key → mock。"""
    if template and os.path.exists(template) and TPL_RULE not in prompt:
        prompt = prompt + " " + TPL_RULE
    if not GEMINI_API_KEY:
        return mock_result(out_path, prompt, preset.get("model", "gemini-2.5-flash-image"), kind)
    try:
        import base64
        import requests

        parts = []
        for r in refs[: int(preset.get("ref_images", 3))]:
            with open(r, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            parts.append({"inline_data": {"mime_type": _mime(r), "data": b64}})
        if template and os.path.exists(template):
            with open(template, "rb") as f:
                t64 = base64.b64encode(f.read()).decode()
            parts.append({"inline_data": {"mime_type": _mime(template), "data": t64}})
        payload = {
            "contents": [{"parts": [{"text": prompt}, *parts]}],
            "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
        }
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent?key={GEMINI_API_KEY}",
            json=payload,
            timeout=240,
        )
        data = resp.json()
        saved = []
        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                if "inlineData" in part:
                    b = base64.b64decode(part["inlineData"]["data"])
                    p = out_path.with_name(f"{out_path.stem}_{len(saved) + 1}{out_path.suffix}")
                    p.write_bytes(b)
                    saved.append(str(p))
        return {"ok": bool(saved), "paths": saved, "model": preset.get("model")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "paths": [], "model": preset.get("model"), "error": str(e)}


# ---------- 视觉理解：参考图 → 文字（提示词识别专用，**不产图**） ----------
# 🔴 与生图分开：这一支只读图出文本，走 OpenAI 兼容的 Chat 接口。
#    真平台「点按钮 → 视觉模型反推提示词」用的就是这类模型。
# 🔑 与生图同一套设计：**多端点 + 失败自动转移**。首选由 VLM_PROVIDER 决定，
#    失败就换下一个端点 —— 这就是后台「节点优先级 / 故障转移」的真实落地。
ARK_CHAT_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
ARTSAPI_CHAT_URL = "https://ai.artsapi.com/v1/chat/completions"

ARK_VLM_MODEL = os.environ.get("ARK_VLM_MODEL", "doubao-seed-1-6-vision-250815")
VLM_MODEL = os.environ.get("VLM_MODEL", "doubao-seed-2-0-pro-260215")
VLM_PROVIDER = os.environ.get("VLM_PROVIDER", "artsapi").strip().lower()


def _vlm_endpoints(override_model: str = "") -> list:
    """视觉理解的端点表（首选在前）。无 key 的端点会被调用方跳过。"""
    eps = [
        {"provider": "artsapi", "url": ARTSAPI_CHAT_URL, "key": ARTSAPI_API_KEY,
         "model": override_model or VLM_MODEL},
        {"provider": "ark", "url": ARK_CHAT_URL, "key": ARK_API_KEY,
         "model": ARK_VLM_MODEL},
    ]
    if VLM_PROVIDER in ("ark", "artsapi"):
        eps.sort(key=lambda e: 0 if e["provider"] == VLM_PROVIDER else 1)
    return eps


def gen_vlm_extract(image_paths: list, instruction: str, model: str = "",
                    max_images: int = 6) -> dict:
    """视觉理解：读参考图 → 返回文本。多端点依次尝试，全失败则 {ok:False}。
    ⛔ 这里**不产占位图** —— 提示词识别要的是文字，没 key 就该由调用方回落到内置规则。"""
    images = []
    for p in list(image_paths)[:max_images]:
        try:
            import base64
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            images.append({"type": "image_url",
                           "image_url": {"url": f"data:{_mime(p)};base64,{b64}"}})
        except Exception:  # noqa: BLE001
            continue
    if not images:
        return {"ok": False, "error": "没有可读的参考图", "tried": []}

    tried = []
    for ep in _vlm_endpoints(model):
        if not ep["key"]:
            tried.append({"provider": ep["provider"], "error": "no_key"})
            continue
        try:
            import requests

            payload = {"model": ep["model"], "temperature": 0.3,
                       "messages": [{"role": "user",
                                     "content": [{"type": "text", "text": instruction}, *images]}]}
            resp = requests.post(
                ep["url"],
                headers={"Authorization": f"Bearer {ep['key']}",
                         "Content-Type": "application/json"},
                json=payload, timeout=120,
            )
            data = resp.json()
            if isinstance(data, dict) and not data.get("error"):
                choices = data.get("choices") or []
                txt = str((choices[0].get("message") or {}).get("content") or "") if choices else ""
                if txt.strip():
                    return {"ok": True, "text": txt, "model": ep["model"],
                            "provider": ep["provider"], "usage": data.get("usage", {}),
                            "tried": tried}
                tried.append({"provider": ep["provider"], "error": "返回为空"})
            else:
                err = (data or {}).get("error") or {}
                msg = str(err.get("code") or err.get("type") or "未知错误") + " " + \
                      str(err.get("message") or "")[:120]
                tried.append({"provider": ep["provider"], "error": msg.strip()})
        except Exception as e:  # noqa: BLE001
            tried.append({"provider": ep["provider"], "error": str(e)[:140]})
    return {"ok": False, "error": "；".join(f"{t['provider']}: {t['error']}" for t in tried)[:400],
            "tried": tried}


ENDPOINTS = {"openai": gen_openai, "seedream": gen_seedream, "gemini": gen_gemini}


# ---------- 联调占位图（无 key 时产出，使链路离线也有图可看） ----------
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(size: int):
    """取一个能渲染中文的字体；找不到就退回 Pillow 默认字体。"""
    from PIL import ImageFont
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def _wrap_cn(text: str, per: int, max_lines: int) -> list:
    """按字符数硬折行（中英混排够用），超出截断加省略号。"""
    text = " ".join(str(text or "").split())
    lines = [text[i:i + per] for i in range(0, len(text), per)]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:max(0, per - 1)] + "…"
    return lines or [""]


# 占位图按**资产类型**区分配色与标签 ——
# 主界面三卡是并排显示的（左＝模特 ‖ 中＝场景 ‖ 右＝成品），
# 若三张长得一模一样，联调态下根本读不出"这格是什么"，也就演示不了"两条支线并行"。
PLACEHOLDER_STYLE = {
    "base_person": {"bg": (238, 243, 252), "grid": (226, 234, 246), "line": (183, 199, 226),
                    "accent": (54, 106, 184), "tag": "模特基础照",
                    "title": "模 特 基 础 照",
                    "sub": "人物支线 · A-pose 素模特 · 不含款式"},
    "base_scene":  {"bg": (235, 246, 241), "grid": (223, 238, 231), "line": (176, 212, 197),
                    "accent": (28, 134, 104), "tag": "初版场景",
                    "title": "初 版 场 景",
                    "sub": "场景支线 · 环境参考 · 不含款式"},
    "final":       {"bg": (252, 245, 236), "grid": (246, 235, 222), "line": (233, 205, 174),
                    "accent": (198, 106, 32), "tag": "成品",
                    "title": "成 品",
                    "sub": "款 × 形象 × 场景 · 组合产出"},
    "shot":        {"bg": (236, 244, 249), "grid": (223, 235, 243), "line": (176, 204, 222),
                    "accent": (32, 112, 152), "tag": "模板镜头",
                    "title": "模 板 镜 头",
                    "sub": "视觉模板 · 同一形象 × 同一场景 × 不同动作"},
    "free_out":    {"bg": (245, 241, 251), "grid": (237, 231, 247), "line": (214, 201, 236),
                    "accent": (116, 82, 190), "tag": "自由出图",
                    "title": "自 由 出 图",
                    "sub": "文生图 / 图生图 · 不走配方链路"},
}
_PLACEHOLDER_DEFAULT = {"bg": (243, 245, 248), "grid": (234, 237, 242), "line": (206, 212, 221),
                        "accent": (120, 128, 140), "tag": "联调",
                        "title": "联 调 占 位 图",
                        "sub": "非真实出图 · 未调用模型 API"}


def _make_placeholder(out_path: Path, prompt: str, model: str, kind: str = ""):
    """生成一张"联调占位图"：明确标注非真实出图，避免与真图混淆。
    kind 决定配色与主标题（base_person / base_scene / final / free_out），
    让三卡并排时一眼读得出差别。返回路径；无 Pillow 时返回 None（退回只写 prompt.txt）。"""
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001
        return None
    try:
        st = PLACEHOLDER_STYLE.get(kind, _PLACEHOLDER_DEFAULT)
        if kind == "shot":
            # 镜头组的图按「动作」区分：把动作提成大标题，15 张缩略一眼各不相同
            segs = [s.strip() for s in str(prompt or "").split("，") if s.strip()]
            act = (segs[-1] if segs else "") or "模板镜头"
            st = dict(st)
            st["title"] = act[:9]
        W, H = 832, 1040
        BG, GRID, LINE, ACC = st["bg"], st["grid"], st["line"], st["accent"]
        TX, TX2, TX3 = (31, 35, 41), (88, 96, 108), (142, 150, 162)
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        for x in range(0, W, 52):
            d.line([(x, 0), (x, H)], fill=GRID, width=1)
        for y in range(0, H, 52):
            d.line([(0, y), (W, y)], fill=GRID, width=1)
        d.rectangle([18, 18, W - 18, H - 18], outline=LINE, width=3)
        d.rectangle([30, 30, W - 30, H - 30], outline=LINE, width=1)
        d.rectangle([18, 18, W - 18, 25], fill=ACC)          # 顶部强调条（缩略图也认得类型）

        f_big, f_mid, f_sm, f_tiny = _font(46), _font(21), _font(18), _font(15)
        f_tag = _font(17)
        tag = st["tag"]                                        # 左上角类型徽章
        tw = d.textlength(tag, font=f_tag)
        d.rounded_rectangle([48, 58, 48 + tw + 36, 100], radius=21, fill=ACC)
        d.text((48 + tw / 2 + 18, 79), tag, font=f_tag, fill=(255, 255, 255), anchor="mm")

        d.text((W / 2, 404), st["title"], font=f_big, fill=ACC, anchor="mm")
        d.text((W / 2, 466), st["sub"], font=f_mid, fill=TX2, anchor="mm")
        d.line([(120, 516), (W - 120, 516)], fill=LINE, width=1)
        d.text((W / 2, 552), "联调占位图 · 非真实出图（未调用模型 API）", font=f_sm, fill=TX2, anchor="mm")
        d.text((W / 2, 592), f"模型：{model}", font=f_sm, fill=TX3, anchor="mm")

        y = 644
        for ln in _wrap_cn(prompt, 30, 4):
            d.text((W / 2, y), ln, font=f_sm, fill=TX3, anchor="mm")
            y += 30
        d.text((W / 2, H - 70), "配置 API key 后，同一条链路自动产出真实图",
               font=f_tiny, fill=TX3, anchor="mm")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, format="PNG")
        return str(out_path)
    except Exception:  # noqa: BLE001
        return None


def mock_result(out_path: Path, prompt: str, model: str, kind: str = "") -> dict:
    """无 key 时的联调模式：**产出"联调占位图"**并保存 prompt，不调任何 API。
    占位图走的是与真图完全相同的落盘/登记/入库路径 —— 配置 key 后无需改任何代码。
    kind（base_person/base_scene/final/free_out）决定占位图配色，联调态下也能区分三卡。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    (out_path.parent / f"{out_path.stem}.prompt.txt").write_text(prompt, encoding="utf-8")
    ph = _make_placeholder(out_path, prompt, model, kind)
    return {"ok": False, "paths": [ph] if ph else [], "model": model, "mock": True,
            "error": f"无 API key，已生成联调占位图（{out_path.name}）并保存 prompt。"}


# ---------- 主流程 ----------
def generate_sku(sku: str, presets_arg: str, tags_arg: str, template: str,
                 prompt_override: str = "", size_override: str = "",
                 count_override: str = "") -> None:
    """prompt_override: 非空时覆盖预设 prompt（仍支持 {material} 等标签占位回填）。
    size_override: 非空时覆盖预设尺寸（如 '2K' 或 '1280x1920'）。
    count_override: 非空时覆盖预设 output（每类图生成张数）。"""
    # 模板解析为完整路径：支持 "tpl_xxx.png"（templates/ 下）或 "templates/tpl_xxx.png"
    if template:
        t = Path(template)
        if not t.is_absolute():
            cand = TEMPLATES_DIR / template
            if cand.exists():
                template = str(cand)
            elif (BASE / template).exists():
                template = str(BASE / template)
    presets = load_presets()
    photos = find_photos(sku)
    if not photos:
        sys.exit(1)
    tags = {}
    for kv in [t for t in tags_arg.split(",") if "=" in t]:
        k, v = kv.split("=", 1)
        tags[k.strip()] = v.strip()

    sku_out = OUT_DIR / sku
    meta = []
    _t0 = __import__("time").time()
    for name in presets_arg.split(","):
        name = name.strip()
        preset = dict(presets.get(name) or {})
        if not preset:
            print(f"[!] 未知预设: {name}（可用: {[k for k in presets if k != 'defaults']}）")
            continue
        if size_override.strip():
            preset["size"] = size_override.strip()
        if count_override.strip():
            preset["output"] = count_override.strip()
        p_dir = sku_out / name
        p_dir.mkdir(parents=True, exist_ok=True)
        base_prompt = prompt_override if prompt_override.strip() else preset.get("prompt", "")
        prompt = build_prompt({"prompt": base_prompt}, tags)
        refs = photos[: int(preset.get("ref_images", 3))]
        n_out = int(preset.get("output", 1))
        src_tag = "自定义" if prompt_override.strip() else "预设"
        print(f"[*] {sku} / {name}: 模型={preset.get('model')} 参考={len(refs)}张 "
              f"prompt={src_tag} size={preset.get('size')} 次数={n_out} → {p_dir}")

        ep = str(preset.get("endpoint", "seedream"))
        fn = ENDPOINTS.get(ep)
        if fn is None:
            print(f"[!] 未知端点: {ep}，跳过 {name}")
            continue
        for i in range(n_out):
            out_path = p_dir / f"{sku}_{name}_{i + 1}.png"
            if ep == "openai":
                r = fn(preset, refs, prompt, out_path)
            else:
                r = fn(preset, refs, prompt, out_path, template)
            status = "ok" if r.get("ok") else ("mock" if r.get("mock") else f"fail:{r.get('error', '')[:60]}")
            meta.append({
                "sku": sku, "preset": name, "idx": i + 1,
                "model": r.get("model", preset.get("model")),
                "provider": r.get("provider", SEEDREAM_PROVIDER),
                "status": status,
                "paths": ";".join(r.get("paths", [])) if r.get("paths") else ("" if r.get("mock") else str(out_path)),
                "prompt": prompt.replace("\n", " ")[:120],
            })

    with open(sku_out / "gen_meta.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["sku", "preset", "idx", "model", "provider", "status", "paths", "prompt"])
        w.writeheader()
        w.writerows(meta)
    _LAST_RUN_SECS = __import__("time").time() - _t0
    globals()["_LAST_RUN_SECS"] = _LAST_RUN_SECS   # 供 workbench 日志记录耗时
    print(f"[✓] {sku} 完成: {len(meta)} 张 耗时 {_LAST_RUN_SECS:.1f}s → {sku_out}（明细 gen_meta.csv）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="AIGC 出图工作台 · 生成引擎（静物 / 细节 / 模特）")
    ap.add_argument("--sku", required=True, help="SKU 编号（对应 photos/<sku>/ 素材目录）")
    ap.add_argument("--presets", default="still_life,detail,onfoot", help="图类型，逗号分隔")
    ap.add_argument("--tags", default="", help="标签回填，如 material=帆布,category=托特包")
    ap.add_argument("--template", default="", help="视觉模板路径（模特图用）")
    args = ap.parse_args()
    generate_sku(args.sku, args.presets, args.tags, args.template)
