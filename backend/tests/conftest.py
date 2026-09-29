"""Entegrasyon testleri: çalışan bir stack'e (api, admin-api, worker, media-worker) karşı koşar.

Lokal çalıştırma (servisler ayaktayken, backend klasöründe):
    ALLOW_PRIVATE_NETWORK=true FORWARDED_ALLOW_IPS=127.0.0.1 pytest -q tests

Notlar:
- Her test oturumu kendi owner hesabını CLI ile oluşturur; mevcut verilere dokunmaz.
- Test görselleri/videoları test sırasında üretilir (depoda ikili dosya yok).
- API ve admin-api'yi FORWARDED_ALLOW_IPS=127.0.0.1 ile başlatın ki X-Forwarded-For testleri çalışsın.
"""
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw, ImageFont

API = os.environ.get("TEST_API_URL", "http://localhost:8000")
ADMIN = os.environ.get("TEST_ADMIN_URL", "http://localhost:8001")
CSRF = {"X-Requested-With": "moderation-panel"}
BACKEND = Path(__file__).resolve().parent.parent


def rand_ip() -> str:
    return f"203.0.{secrets.randbelow(250) + 1}.{secrets.randbelow(250) + 1}"


class Admin:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.client = httpx.Client(base_url=ADMIN, headers=CSRF | {"X-Forwarded-For": rand_ip()}, timeout=30)

    def login(self):
        r = self.client.post("/auth/login", json={"email": self.username, "password": self.password})
        r.raise_for_status()
        return r.json()

    def get(self, path, **kw):
        return self.client.get(path, **kw)

    def post(self, path, json=None, **kw):
        return self.client.post(path, json=json if json is not None else {}, **kw)

    def patch(self, path, json, **kw):
        return self.client.patch(path, json=json, **kw)

    def delete(self, path, json=None, **kw):
        return self.client.request("DELETE", path, json=json, **kw)


def cli(*args: str) -> str:
    out = subprocess.run([sys.executable, "-m", "app.cli", *args], cwd=BACKEND, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def create_admin(role: str = "owner") -> Admin:
    username = f"t.{role}.{secrets.token_hex(4)}@moderasyon-test.com"
    out = cli("create-admin", "--email", username, "--role", role)
    temp = re.search(r"^\s{2}(\S+)\s*$", out, re.M).group(1)
    admin = Admin(username, temp)
    admin.login()
    new = "Test-" + secrets.token_urlsafe(12) + "9aA"
    r = admin.post("/auth/change-password", {"current_password": temp, "new_password": new})
    assert r.status_code == 200, r.text
    admin.password = new
    return admin


@pytest.fixture(scope="session")
def owner() -> Admin:
    admin = create_admin("owner")
    # Testler deterministik olsun: AI varsayılan olarak kapalı; AI testleri kendi içinde açar
    admin.patch("/ai/settings", {"ai_enabled": False})
    time.sleep(6)   # işçilerin ayar önbelleği
    return admin


def api_client(key: str, ip: str | None = None) -> httpx.Client:
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {key}", "X-Forwarded-For": ip or rand_ip()},
                        timeout=60)


@pytest.fixture(scope="session")
def project(owner):
    slug = f"test-{secrets.token_hex(4)}"
    r = owner.post("/projects", {"name": f"Test {slug}", "slug": slug, "key_environment": "test"})
    assert r.status_code == 201, r.text
    data = r.json()
    # Mevcut testler üç adımlı akışı (inceleme kuyruğu) kullanır
    owner.client.put(f"/projects/{data['id']}/policy", json={"decision_mode": "three_step", "ai_mode": "off"})
    yield {"id": data["id"], "key": data["api_key"], "slug": slug}
    owner.delete(f"/projects/{data['id']}", {"confirm_slug": slug})


@pytest.fixture()
def api(project) -> httpx.Client:
    return api_client(project["key"])


def wait_final(client: httpx.Client, public_id: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/v1/moderate/{public_id}").json()
        if body["status"] in ("completed", "failed"):
            return body
        time.sleep(0.3)
    raise AssertionError(f"{public_id} zamanında tamamlanmadı")


# ---------------------------------------------------------------- test medyası

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _scene(seed: int, size=(800, 600)) -> Image.Image:
    import random
    rnd = random.Random(seed)
    im = Image.new("RGB", size, (rnd.randint(40, 200), rnd.randint(40, 200), rnd.randint(40, 200)))
    d = ImageDraw.Draw(im)
    for _ in range(25):
        x, y, r = rnd.randint(0, size[0] - 40), rnd.randint(0, size[1] - 40), rnd.randint(10, 80)
        d.ellipse((x, y, x + r, y + r), fill=(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)))
    return im


def _caption(im: Image.Image, text: str) -> Image.Image:
    im = im.copy()
    d = ImageDraw.Draw(im)
    font = ImageFont.truetype(FONT, 64) if Path(FONT).exists() else ImageFont.load_default()
    d.rectangle((40, 220, 760, 380), fill=(255, 255, 255))
    d.text((60, 260), text, font=font, fill=(0, 0, 0))
    return im


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("media")
    files: dict[str, Path] = {}

    files["clean"] = root / "clean.jpg"
    _scene(1).save(files["clean"], quality=90)

    files["meme"] = root / "meme.jpg"
    _caption(_scene(2), "SİKTİR GİT AMK").save(files["meme"], quality=90)

    files["mild"] = root / "mild.png"
    _caption(_scene(3), "SALAK HERİF").save(files["mild"])
    # Aynı görselin küçültülmüş + yeniden sıkıştırılmış kopyası (engel listesi testi)
    files["mild_copy"] = root / "mild_copy.jpg"
    Image.open(files["mild"]).resize((640, 480)).save(files["mild_copy"], quality=70)

    frames = [_scene(10 + i) for i in range(6)]
    frames[4] = _caption(frames[4], "ORUSPU ÇOCUĞU")  # sadece 5. karede
    files["gif"] = root / "anim.gif"
    frames[0].save(files["gif"], save_all=True, append_images=frames[1:], duration=500, loop=0)

    for i in range(12):
        f = _scene(100 + i)
        if i == 7:
            f = _caption(f, "SİKTİR GİT AMK")
        f.save(root / f"v_{i:02d}.png")
    files["video"] = root / "video.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", "1", "-i", str(root / "v_%02d.png"),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "25", str(files["video"])], check=True, timeout=120)

    files["bomb"] = root / "bomb.png"
    Image.new("1", (20000, 20000)).save(files["bomb"])

    files["fake"] = root / "fake.jpg"
    files["fake"].write_bytes(b"<html><script>alert(1)</script></html>")
    return files
