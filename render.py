"""HTML -> PDF conversion using a locally installed Chromium/Chrome binary."""

import os
import shutil
import subprocess
import tempfile

CHROME_BIN_CANDIDATES = [
    os.environ.get("CHROME_BIN", ""),
    os.environ.get("CHROMIUM_BIN", ""),
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


def _chrome_bin() -> str:
    for p in CHROME_BIN_CANDIDATES:
        if p and os.path.exists(p):
            return p
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("Chrome/Chromium 실행 파일을 찾을 수 없습니다.")


def html_to_pdf(html_str: str, pdf_path: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html_str)
        html_path = f.name
    try:
        cmd = [
            _chrome_bin(),
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            f"file://{html_path}",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if r.returncode != 0 and not os.path.exists(pdf_path):
            raise RuntimeError(f"Chrome PDF 변환 실패: {r.stderr}")
        return pdf_path
    finally:
        try:
            os.unlink(html_path)
        except Exception:
            pass
