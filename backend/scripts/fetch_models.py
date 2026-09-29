"""Docker build sırasında çalışır: daha hassas NudeNet 640m modelini indirmeyi dener.

İnmezse veya dosya geçerli bir model değilse silinir; sistem pakete gömülü 320n modeliyle çalışır.
Build bu betik yüzünden asla başarısız olmaz. Çalışma anında internete çıkılmaz.
"""
import os
import urllib.request

path = os.environ.get("NUDENET_MODEL_PATH", "/models/640m.onnx")
url = os.environ.get("NUDENET_640M_URL", "")
os.makedirs(os.path.dirname(path), exist_ok=True)
try:
    urllib.request.urlretrieve(url, path)
    import onnxruntime
    onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"])
    print("NudeNet 640m hazır:", os.path.getsize(path), "bayt")
except Exception as exc:  # noqa: BLE001
    if os.path.exists(path):
        os.remove(path)
    print("NudeNet 640m indirilemedi, 320n kullanılacak:", type(exc).__name__)

from nudenet import NudeDetector  # noqa: E402

NudeDetector()
print("nudenet ok")
