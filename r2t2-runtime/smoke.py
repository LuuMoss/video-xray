import sys, time, wave
from pathlib import Path
sys.path.insert(0, r"C:\Users\Moss\AppData\Local\hermes\asr-r2t2\core")
import numpy as np, soxr
from confucius.native import NativeQ8Engine
from confucius.vad import FireRedOnnxVAD
from confucius.segmented import SegmentedStream
D = r"C:\Users\Moss\AppData\Local\hermes\asr-r2t2"
t0 = time.time()
eng = NativeQ8Engine(D + r"\models", D + r"\runtime\native-cpu", n_threads=4, gpu_layers=0, hash_files=False)
print("engine load %.1fs" % (time.time() - t0))
w = wave.open(r"C:\Users\Moss\AppData\Local\hermes\voices\xiaoya_ref_loud.wav")
data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
data = data.reshape(-1, w.getnchannels()).mean(axis=1)
data = soxr.resample(data, w.getframerate(), 16000)
print("audio %.1fs -> 16k mono %.1fs" % (w.getnframes() / w.getframerate(), len(data) / 16000))
st = SegmentedStream(eng, language="Chinese", offline=True, vad=FireRedOnnxVAD(Path(D + r"\models\FireRedVAD-ONNX")))
t1 = time.time()
st.feed(data)
fin = st.finish()
for s in fin["segments"]:
    print("[%.2f-%.2f] %s" % (s["start_sample"] / 16000, s["end_sample"] / 16000, s["text"]))
print("transcribe %.1fs for %.1fs audio" % (time.time() - t1, len(data) / 16000))
