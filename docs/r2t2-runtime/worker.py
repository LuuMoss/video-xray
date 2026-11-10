"""Confucius4-R2T2 转写工人（CPU）：stdin WAV(16k mono) -> stdout JSON 末行
[{"s":起始秒,"e":结束秒,"text":文本}]，转完进程即退，零显存。

由 video-xray 插件 audio.py 拉起。env 旋钮（面板参数保存后注入）：
  VA_R2T2_THREADS  引擎线程数（默认 4，主人定的 CPU 配方一部分）
  VA_R2T2_HOTWORDS 热词/上下文串，注入解码 prompt
  VA_R2T2_DEVICE   cpu|cuda（默认 cpu）-> runtime/native-cpu|native-cuda
  VA_R2T2_QUANT    Q8_0|Q4_K_M（默认 Q8_0）
  VA_R2T2_LANG     语言名；空或 Auto = 自动检测
"""
import io
import json
import os
import sys
import threading
import time
import wave
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "core"))

import numpy as np  # noqa: E402
from confucius.native import NativeQ8Engine, SUPPORTED_LANGUAGES  # noqa: E402
from confucius.segmented import SegmentedStream  # noqa: E402
from confucius.vad import FireRedOnnxVAD  # noqa: E402

RATE = 16000


def _env(key, default=""):
    v = (os.environ.get(key) or "").strip()
    return v or default


def _engine():
    """按设备/量化旋钮建引擎。

    GPU 契约：CUDA 运行树 runtime/native-cuda 独立于 CPU 树；选了 cuda 而
    树不在就硬失败报清原因——绝不静默回落 CPU，面板用户是故意选的 GPU。
    """
    dev = _env("VA_R2T2_DEVICE", "cpu").lower()
    if dev not in ("cpu", "cuda"):
        dev = "cpu"
    quant = _env("VA_R2T2_QUANT", "Q8_0").upper()
    if quant not in ("Q8_0", "Q4_K_M"):
        quant = "Q8_0"
    build = Path(_HERE) / "runtime" / ("native-cuda" if dev == "cuda" else "native-cpu")
    if dev == "cuda" and not (build / "python").is_dir():
        raise SystemExit("native-cuda runtime not installed "
                         "(expected runtime/native-cuda); pick CPU or install it")
    return NativeQ8Engine(
        Path(_HERE) / "models", build,
        n_threads=int(_env("VA_R2T2_THREADS", "4")),
        gpu_layers=(-1 if dev == "cuda" else 0),
        hash_files=False, quantization=quant)


def _language():
    lang = _env("VA_R2T2_LANG")
    if not lang or lang.lower() == "auto":
        return None                       # SegmentedStream 默认 = 自动检测
    return lang if lang in SUPPORTED_LANGUAGES else None


def main():
    raw = sys.stdin.buffer.read()
    if not raw:
        return
    with wave.open(io.BytesIO(raw), "rb") as w:
        if w.getnchannels() != 1 or w.getframerate() != RATE:
            raise SystemExit(f"expect 16k mono wav, got {w.getframerate()}Hz x{w.getnchannels()}ch")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        data = data.astype(np.float32) / 32768.0
    if not np.any(np.abs(data) > 5e-3):        # 全静音免加载引擎
        return
    eng = _engine()
    st = SegmentedStream(eng, language=_language(),
                         context=(os.environ.get("VA_R2T2_HOTWORDS") or ""),
                         offline=True,
                         vad=FireRedOnnxVAD(Path(_HERE) / "models" / "FireRedVAD-ONNX"))
    st.feed(data)
    fin = st.finish()
    cues = [{"s": round(s["start_sample"] / RATE, 2), "e": round(s["end_sample"] / RATE, 2),
             "text": (s["text"] or "").strip()}
            for s in fin["segments"] if (s["text"] or "").strip()]
    print(json.dumps(cues, ensure_ascii=False))


def probe():
    """面板「保存」的真机加载验证（--probe）：不读 stdin，强行加载
    Q8 引擎 + FireRedVAD 模型再退出——关键是真把模型文件加载一遍：喂
    静音会被 VAD 短路、根本不碰模型，验证就成了假绿。打印 ["OK"] 走
    stdout（JSON 数组，_run_worker 照常解析），进程退出即归还全部资源。
    任何一步失败（缺文件、DLL 死、binding 版本不合）异常退出 rc!=0，
    stderr 就是原因。"""
    eng = _engine()
    FireRedOnnxVAD(Path(_HERE) / "models" / "FireRedVAD-ONNX")
    del eng
    print(json.dumps(["OK"]))


if __name__ == "__main__":
    t0 = time.time()
    # Polite cancel (owner 2026-11-10): when the user cancels, the parent
    # touches VA_CANCEL_FILE and this poll thread calls os._exit(0).
    # os._exit goes through the normal ExitProcess teardown, so the CUDA
    # driver reclaims the context synchronously and VRAM drops at once --
    # TerminateProcess (hard kill mid-kernel) instead left a deferred
    # "ghost" block that only cleared under allocation pressure (e.g. the
    # next chat request squeezing the main engine).
    _cancel_file = _env("VA_CANCEL_FILE")
    if _cancel_file:
        def _watch_cancel():
            while True:
                try:
                    if os.path.exists(_cancel_file):
                        os._exit(0)
                except OSError:
                    pass
                time.sleep(0.2)
        threading.Thread(target=_watch_cancel, daemon=True).start()
    if "--probe" in sys.argv:
        probe()
    else:
        main()
    print(f"[r2t2 worker] {time.time() - t0:.1f}s", file=sys.stderr)
