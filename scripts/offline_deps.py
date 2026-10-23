"""离线依赖安装脚本（pip 用不了时的备用方案）。

用法：
    python scripts/offline_deps.py            # 下载并解包到 ./.deps
    set PYTHONPATH=./.deps && python run.py   # Windows

它会直接从 PyPI 下载本项目所需的 wheel（纯 Python 包 + 对应平台的二进制包），
就地解包到 .deps，不需要 pip、不需要虚拟环境、不需要管理员权限。

正常情况下你只要 `pip install -r requirements.txt` 就够了，用不到这个脚本。
"""

from __future__ import annotations

import json
import platform
import sys
import urllib.request
import zipfile
from pathlib import Path

# 本项目运行所需的完整依赖树（含传递依赖）
PACKAGES = [
    "httpx",
    "httpcore",
    "h11",
    "anyio",
    "sniffio",
    "certifi",
    "idna",
    "typing_extensions",
    "websockets",
    "PyYAML",
]

ROOT = Path(__file__).resolve().parent.parent
WHEEL_DIR = ROOT / "wheels"
DEPS_DIR = ROOT / ".deps"

PY_TAG = f"cp{sys.version_info.major}{sys.version_info.minor}"


def _platform_tags() -> list[str]:
    machine = platform.machine().lower()
    if sys.platform == "win32":
        arch = "win_amd64" if machine in ("amd64", "x86_64") else "win32"
        return [f"{PY_TAG}-{PY_TAG}-{arch}", f"{PY_TAG}-abi3-{arch}", "py3-none-any"]
    if sys.platform == "darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
        return [f"{PY_TAG}-{PY_TAG}-macosx_11_0_{arch}", f"{PY_TAG}-abi3-macosx_11_0_{arch}", "py3-none-any"]
    arch = "aarch64" if machine in ("aarch64", "arm64") else "x86_64"
    return [f"{PY_TAG}-{PY_TAG}-manylinux_2_17_{arch}", f"{PY_TAG}-{PY_TAG}-linux_{arch}",
            f"{PY_TAG}-abi3-manylinux_2_17_{arch}", "py3-none-any"]


def _pick_wheel(files: list[dict], tags: list[str]) -> dict | None:
    for tag in tags:
        for f in files:
            if f["packagetype"] != "bdist_wheel" or f.get("yanked"):
                continue
            name = f["filename"]
            # 形如 websockets-15.0.1-cp312-cp312-win_amd64.whl
            if name.endswith(f"-{tag}.whl"):
                return f
    return None


def main() -> int:
    WHEEL_DIR.mkdir(exist_ok=True)
    DEPS_DIR.mkdir(exist_ok=True)
    tags = _platform_tags()
    print(f"Python {PY_TAG} / {sys.platform} -> 目标标签 {tags}")

    for pkg in PACKAGES:
        try:
            with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=30) as resp:
                meta = json.load(resp)
        except Exception as exc:  # noqa: BLE001
            print(f"  [跳过] {pkg}: 查询 PyPI 失败 ({exc})")
            continue

        wheel = _pick_wheel(meta.get("urls", []), tags)
        if wheel is None:
            print(f"  [跳过] {pkg}: 没有匹配当前平台的 wheel")
            continue

        target = WHEEL_DIR / wheel["filename"]
        if not target.is_file():
            print(f"  下载 {wheel['filename']} …", end="", flush=True)
            try:
                urllib.request.urlretrieve(wheel["url"], target)
            except Exception as exc:  # noqa: BLE001
                print(f" 失败: {exc}")
                continue
            print(" 完成")
        with zipfile.ZipFile(target) as zf:
            zf.extractall(DEPS_DIR)
        print(f"  解包 {pkg} {meta['info']['version']} -> {DEPS_DIR}")

    print("\n完成。启动方式：")
    if sys.platform == "win32":
        print(f'  set PYTHONPATH={DEPS_DIR}')
        print("  python run.py")
    else:
        print(f"  PYTHONPATH={DEPS_DIR} python run.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
