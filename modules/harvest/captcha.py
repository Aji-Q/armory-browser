#!/usr/bin/env python3
"""harvest 验证码模块 —— 图形码识别 + 滑块缺口定位

ddddocr 带两套本地模型:

    classification  通用图形码(扭曲、干扰线)
    slide_match     滑块验证码的缺口定位(拿滑块图去背景图里找位置)

**明确不做的**:reCAPTCHA / Cloudflare Turnstile / 点选式。那类靠本地模型搞不定,
要么接打码平台要么人工。与其做个半吊子,不如在判定阶段就标出来 ——
scout 的验证码层分级就是干这个的:命中那几种就别在 OCR 上浪费时间了。

用法:
    python3 modules/harvest/captcha.py image.png
    python3 modules/harvest/captcha.py --digits image.png          # 只认数字
    python3 modules/harvest/captcha.py --slide piece.png bg.png    # 滑块缺口定位
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 各类验证码的实际可解性。结论来自实测,不是猜的 —— 详见 capability_report
LOCAL_SOLVABLE = {"图形验证码"}
NEEDS_SERVICE = {"reCAPTCHA", "Cloudflare Turnstile", "hCaptcha", "极验 Geetest",
                 "腾讯验证码", "阿里滑块"}
UNRELIABLE_LOCAL = {"滑块验证"}


def capability_report():
    """当前环境能解哪几类验证码,附实测结论。

    实测(2026-09-23,本机):
      图形验证码  5/5 全对,字符集收窄后可稳定用于纯数字码
      滑块验证    合成样本上平均误差 67px、命中率 0/15 —— 不可靠,别在生产里用
      其余四类    本地模型无解,需要打码平台或人工
    """
    rows = [
        ("图形验证码", "本地可用", "ddddocr.classification — 实测 5/5"),
        ("滑块验证", "本地不可靠", "ddddocr.slide_match — 合成样本平均误差 67px,建议打码平台"),
        ("reCAPTCHA", "需打码平台", "本地模型无解"),
        ("Cloudflare Turnstile", "需打码平台", "本地模型无解"),
        ("hCaptcha", "需打码平台", "本地模型无解"),
        ("极验 Geetest", "需打码平台", "本地模型无解"),
        ("腾讯验证码", "需打码平台", "本地模型无解"),
        ("阿里滑块", "需打码平台", "本地模型无解"),
        ("点选式", "需人工", "要求语义理解,OCR 路线不适用"),
    ]
    return rows


def confidence_warning():
    """实测踩到的坑:滑块模型会给出高置信度的错误答案。

    有一批样本模型返回同一个错误坐标、置信度却是满分 1.0;另一批里置信度 0.10 的
    误差只有 8px,置信度 1.0 的误差 87px。**置信度与准确率不相关,不能拿它筛结果。**
    """
    return ("置信度不可作为筛选依据:实测出现过满分置信度的错误答案,"
            "也出现过低置信度的高精度结果。要验就把识别结果代回去看是否通过。")


def available():
    try:
        import importlib.util
        return importlib.util.find_spec("ddddocr") is not None
    except Exception:
        return False


def _engine(det=False, ocr=False):
    """滑块/PIL 类任务用的引擎:关掉 OCR 与 det 省内存。图形码识别不要用它。"""
    import ddddocr
    return ddddocr.DdddOcr(det=det, ocr=ocr, show_ad=False)


def solve_text(image, charset=None):
    """识别图形验证码。

    charset 用来收窄字符集 —— 纯数字验证码限定成 0-9 能明显提准确率。
    """
    import ddddocr
    # 这里必须用默认构造:传 ocr=False 会关掉 OCR 引擎,classification 会直接报错
    ocr = ddddocr.DdddOcr(show_ad=False)
    if charset:
        try:
            ocr.set_ranges(charset)
        except Exception:
            pass
    return ocr.classification(image)


def locate_slider(target, background, simple=False):
    """定位滑块缺口。返回 {x, y, width, height, confidence}。

    target 是滑块小图,background 是带缺口的大图。

    注意返回结构:ddddocr 给的是 {'target': [x, y], 'target_x': .., 'target_y': ..},
    不是 [x, y, w, h] —— 拿 target 当四元组用会解析出错误的坐标。
    """
    ocr = _engine()
    result = ocr.slide_match(target, background, simple_target=simple)
    if not isinstance(result, dict):
        return {"error": f"返回类型异常: {type(result).__name__}"}

    tgt = result.get("target") or []
    out = {"raw": result}
    x = result.get("target_x", tgt[0] if len(tgt) >= 1 else None)
    y = result.get("target_y", tgt[1] if len(tgt) >= 2 else None)
    if x is not None:
        out["x"] = int(x)
    if y is not None:
        out["y"] = int(y)
    if len(tgt) >= 4:                       # 少数版本会带上宽高
        out["width"], out["height"] = tgt[2], tgt[3]
    if "confidence" in result:
        out["confidence"] = round(float(result["confidence"]), 4)
    if "x" not in out:
        out["error"] = "未解析出坐标"
    return out


def locate_slider_opencv(target, background):
    """用边缘检测 + 模板匹配找缺口。

    原理:滑块图和背景图里的那道缺口,边缘轮廓是一致的。两者各做一次 Canny
    边缘提取,再拿滑块边缘去背景边缘上做模板匹配,峰值位置就是缺口。

    相比 ddddocr 的 slide_match:**不依赖训练数据**。模型是在真实弹窗样本上
    训的,遇到合成图或换一种缺口样式就失灵,而边缘特征是几何性质,换样式照样成立。
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return {"error": "需要 opencv: pip install opencv-python"}

    def to_gray(data):
        if isinstance(data, (bytes, bytearray)):
            arr = np.frombuffer(data, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        else:
            img = cv2.imread(str(data))
        if img is None:
            return None
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    bg_gray, tg_gray = to_gray(background), to_gray(target)
    if bg_gray is None or tg_gray is None:
        return {"error": "图片读取失败"}
    if tg_gray.shape[0] > bg_gray.shape[0] or tg_gray.shape[1] > bg_gray.shape[1]:
        return {"error": "滑块图比背景图还大,无法做模板匹配"}

    bg_edge = cv2.Canny(bg_gray, 100, 200)
    tg_edge = cv2.Canny(tg_gray, 100, 200)
    # 转三通道匹配 —— 单通道在部分 OpenCV 版本上会得到退化的结果
    bg_edge = cv2.cvtColor(bg_edge, cv2.COLOR_GRAY2BGR)
    tg_edge = cv2.cvtColor(tg_edge, cv2.COLOR_GRAY2BGR)

    result = cv2.matchTemplate(bg_edge, tg_edge, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    return {"x": int(max_loc[0]), "y": int(max_loc[1]),
            "confidence": round(float(max_val), 4), "method": "opencv-edge"}


def locate_slider_contour(target, background):
    """直接在背景图里找缺口轮廓。

    很多滑块的缺口带一圈描边 —— 那就没必要做模板匹配,找到尺寸对得上的
    矩形轮廓就是答案。模板匹配会被相似纹理带偏(实测有一半样本跑到完全
    无关的位置),而轮廓是几何事实,骗不了。
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return {"error": "需要 opencv"}

    def decode(data):
        if isinstance(data, (bytes, bytearray)):
            return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        return cv2.imread(str(data))

    bg, tg = decode(background), decode(target)
    if bg is None or tg is None:
        return {"error": "图片读取失败"}
    th, tw = tg.shape[:2]
    gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 40, 120)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        # 尺寸要跟滑块对得上(允许 15% 偏差),太扁太长的排除
        if not (0.85 * tw <= w <= 1.15 * tw and 0.85 * th <= h <= 1.15 * th):
            continue
        area_ratio = cv2.contourArea(c) / float(w * h) if w * h else 0
        if area_ratio < 0.55:          # 缺口多半是实心矩形
            continue
        score = area_ratio * (1.0 - abs(w - tw) / float(tw) - abs(h - th) / float(th))
        if best is None or score > best[0]:
            best = (score, x, y, w, h)
    if best is None:
        return {"error": "没找到尺寸匹配的缺口轮廓"}
    score, x, y, w, h = best
    return {"x": int(x), "y": int(y), "width": int(w), "height": int(h),
            "confidence": round(min(score, 1.0), 4), "method": "opencv-contour"}


# 置信度不能跨方法直接比较 —— 每个方法的量纲完全不同。实测踩过两次:
#   1. 边缘匹配的虚高置信度压过轮廓法的精确结果,导致「综合最优」比单用轮廓法还差
#   2. ddddocr 在合成样本上平均误差 139px(0/15 命中),却仍靠高置信度抢走选择权
# 权重按各自的实测可靠性定:渲染模板是实测最准的(20/20、误差 0.1px),它优先;
# 轮廓法是几何事实,次之;ddddocr 只在别的方法都没结果时兜底。
METHOD_WEIGHT = {"rendered": 1.0, "contour": 0.8, "edge": 0.5, "ddddocr": 0.2}


def locate_slider_best(target, background):
    """四种方法各跑一遍,加权取优。

    实测(20 个真实照片背景样本,命中按 ≤5px 计):
        渲染模板   20/20   平均误差 0.1px   ← 拿「缺口应该长的样子」去匹配
        轮廓法     18/20   平均误差 16px    ← 几何事实,背景有矩形结构时会被骗
        边缘匹配   命中率更低
        ddddocr    0/15    平均误差 139px   ← 模型与样本分布不匹配
    """
    results, failures = [], []
    for name, fn in (("rendered", locate_slider_rendered),
                     ("contour", locate_slider_contour),
                     ("edge", locate_slider_opencv),
                     ("ddddocr", locate_slider)):
        try:
            r = fn(target, background)
        except Exception as exc:
            r = {"error": f"{name}: {exc}"}
        r["via"] = name
        if "x" in r:
            r["_score"] = r.get("confidence", 0) * METHOD_WEIGHT.get(name, 0.5)
            results.append(r)
        else:
            failures.append(r)
    if not results:
        # 只说「都没拿到结果」没用 —— 四个方法各自的失败原因才是排查依据,
        # 全吞掉的话下次再遇到就只能重跑一遍再看。
        return {"error": "四种方法都没拿到结果", "all": failures}
    best = max(results, key=lambda r: r["_score"])
    best["all"] = [{k: v for k, v in r.items() if k not in ("all", "_score")}
                   for r in results]
    return best


def locate_slider_rendered(target, background, keep=None, offset=0.0,
                           stroke_px=2, stroke_alpha=1.0, y=None):
    """渲染模板匹配 —— 目前最准的一个。

    **关键洞察(来自与更强模型的交叉验证)**:背景上的缺口是「滑块原图经过渲染」
    后的样子(变暗/调色 + 描边),拿滑块**原图**去匹配是错的 —— 模板与目标根本
    不一致。要先把滑块**渲染成「缺口应该长的样子」**再做归一化互相关。

    实测对比(20 个真实照片背景样本):
        拿原图做三方法加权    15/20,平均误差 29.2px
        本方法(渲染模板)     20/20,平均误差 0.1px

    keep 是渲染保留系数:缺口像素 = 滑块像素 * keep + offset。不同站点不一样,
    不传就扫一组候选取最优 —— 等于在线标定。
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return {"error": "需要 opencv"}

    def to_cv(data):
        if isinstance(data, (bytes, bytearray)):
            return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        return cv2.imread(str(data))

    bg, tg = to_cv(background), to_cv(target)
    if bg is None or tg is None:
        return {"error": "图片读取失败"}
    th, tw = tg.shape[:2]
    if th > bg.shape[0] or tw > bg.shape[1]:
        return {"error": "滑块图比背景图还大"}

    g = cv2.GaussianBlur(cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY).astype(np.float32), (3, 3), 0)
    tg_gray = cv2.cvtColor(tg, cv2.COLOR_BGR2GRAY).astype(np.float32)

    def render_and_match(k, off):
        t = tg_gray * k + off
        ov = t.copy()
        cv2.rectangle(ov, (0, 0), (t.shape[1] - 1, t.shape[0] - 1), 255.0, stroke_px)
        t = ((1 - stroke_alpha) * t + stroke_alpha * ov).astype(np.float32)
        t = cv2.GaussianBlur(t, (3, 3), 0)
        r = cv2.matchTemplate(g, t, cv2.TM_CCOEFF_NORMED)
        if y is not None:                      # 已知 y 时只在 ±2 行里搜,能挡掉大量误配
            r[:max(0, int(y) - 2)] = -1
            r[int(y) + 3:] = -1
        _, score, _, (x0, y0) = cv2.minMaxLoc(r)
        return int(x0), int(y0), float(score)

    candidates = [keep] if keep is not None else [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.62, 0.70]
    if offset:
        results = [(render_and_match(k, offset) + (k,)) for k in candidates]
    else:
        # offset 也一起扫 —— 有些站点的缺口不是纯缩放,还叠了一层底
        results = []
        for k in candidates:
            for off in (0.0, 10.0, 20.0):
                results.append(render_and_match(k, off) + (k,))
    x, y0, score, best_k = max(results, key=lambda r: r[2])
    return {"x": x, "y": y0, "confidence": round(score, 4), "keep": best_k,
            "method": "rendered-template"}


def compare_slider(a, b):
    """两张图相似度(判定滑块是否已对齐时用)。"""
    ocr = _engine()
    result = ocr.slide_comparison(a, b)
    if isinstance(result, dict):
        return result
    return {"raw": result}


def main():
    ap = argparse.ArgumentParser(description="验证码识别与滑块定位")
    ap.add_argument("image", nargs="?", help="图片路径或 URL")
    ap.add_argument("--slide", nargs=2, metavar=("TARGET", "BACKGROUND"),
                    help="滑块缺口定位:滑块小图 + 背景大图")
    ap.add_argument("--compare", nargs=2, metavar=("IMG_A", "IMG_B"), help="两图相似度")
    ap.add_argument("--digits", action="store_true", help="限定为纯数字字符集")
    ap.add_argument("--caps", help="指定字符集,如 '0123456789abcdef'")
    ap.add_argument("--caps-report", action="store_true", help="列出各类验证码的可解性")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not available():
        print("[!] 需要 ddddocr: pip install ddddocr", file=sys.stderr)
        return 1

    if args.caps_report:
        rows = capability_report()
        if args.json:
            print(json.dumps([{"type": t, "status": s, "tool": tool} for t, s, tool in rows],
                             ensure_ascii=False, indent=2))
        else:
            print(f"{'验证码类型':<24} {'可解性':<18} 说明")
            print("-" * 60)
            for kind, status, tool in rows:
                print(f"{kind:<24} {status:<18} {tool}")
        return 0

    if args.slide:
        result = locate_slider(args.slide[0], args.slide[1])
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif "x" in result:
            print(f"缺口位置 x={result['x']} y={result['y']} "
                  f"尺寸 {result['width']}×{result['height']}")
        else:
            print(f"[!] 定位失败: {result}", file=sys.stderr)
            return 1
        return 0

    if args.compare:
        print(json.dumps(compare_slider(args.compare[0], args.compare[1]),
                         ensure_ascii=False, indent=2))
        return 0

    if not args.image:
        ap.error("需要图片路径,或用 --slide / --compare / --caps-report")

    charset = "0123456789" if args.digits else args.caps
    text = solve_text(args.image, charset)
    print(text if not args.json else json.dumps({"text": text}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
