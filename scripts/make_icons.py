"""PWA 아이콘 생성: scripts/icon.svg 를 Chrome(헤드리스)으로 PNG 로 렌더링한다. 결과물은 static/ 에 커밋돼 있다.
실행(macOS): .venv/bin/python scripts/make_icons.py   (Google Chrome 과 기본 포함된 sips 필요)
maskable 안전영역을 위해 그림은 중앙 약 60% 안에 두고 배경은 가장자리까지 채운다.
"""
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "scripts" / "icon.svg"
OUT = ROOT / "static"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SIZES = (("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180))


def render_512(path):
    with tempfile.TemporaryDirectory() as d:
        page = Path(d) / "icon.html"
        page.write_text(f'<html><body style="margin:0"><img src="file://{SVG}" width="512" height="512" style="display:block"></body></html>')
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=512,512",
                        "--force-device-scale-factor=1", "--allow-file-access-from-files",
                        f"--screenshot={path}", f"file://{page}"], check=True, capture_output=True)


def resize(src, dst, size):
    subprocess.run(["sips", "-z", str(size), str(size), str(src), "--out", str(dst)], check=True, capture_output=True)


if __name__ == "__main__":
    big = OUT / "icon-512.png"
    render_512(big)
    for name, size in SIZES:
        if size != 512:
            resize(big, OUT / name, size)
        print("wrote", OUT / name, size)
