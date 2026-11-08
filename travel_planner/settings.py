"""本地配置：高德 key、Ollama 模型与地址。网页版可以在设置页里直接改。

为什么放在 .env：
从 启动网页版.cmd 双击启动时，进程不会继承你临时设的环境变量，
所以配置必须落在文件里；`.env` 已在 .gitignore，不会被提交。
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

DEFAULT_MODEL = "qwen3:8b"
DEFAULT_HOST = "http://127.0.0.1:11434"
MANAGED_KEYS = ("AMAP_KEY", "TRAVEL_MODEL", "OLLAMA_HOST")


def load_env_file(path: Path | None = None) -> dict:
    """把 .env 里的 KEY=VALUE 读进 os.environ（已存在的环境变量优先）。"""
    target = path or ENV_FILE
    loaded = {}
    if not target.exists():
        return loaded
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return loaded
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or not value:
            continue
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def read_env(path: Path | None = None) -> dict:
    """只读 .env（不碰 os.environ），用于回显当前配置。"""
    target = path or ENV_FILE
    result = {}
    if not target.exists():
        return result
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def write_env(updates: dict, path: Path | None = None) -> Path:
    """合并写入 .env：只覆盖给定的键，其它行原样保留。"""
    target = path or ENV_FILE
    updates = {key: str(value) for key, value in updates.items() if value is not None}
    lines = []
    if target.exists():
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
    index = {}
    for position, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        index[stripped.split("=", 1)[0].strip()] = position
    for key, value in updates.items():
        if key in index:
            lines[index[key]] = f"{key}={value}"
        else:
            lines.append(f"{key}={value}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    for key, value in updates.items():
        os.environ[key] = value
    return target


# ---------- 读取当前配置 ----------
def amap_key() -> str | None:
    load_env_file()
    return os.environ.get("AMAP_KEY") or None


def model() -> str:
    load_env_file()
    return os.environ.get("TRAVEL_MODEL") or os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL


def ollama_host() -> str:
    load_env_file()
    return (os.environ.get("OLLAMA_HOST") or DEFAULT_HOST).rstrip("/")


def mask(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def current() -> dict:
    key = amap_key()
    return {
        "amap_key_masked": mask(key),
        "amap_configured": bool(key),
        "model": model(),
        "ollama_host": ollama_host(),
        "env_file": str(ENV_FILE),
    }


# ---------- 校验 ----------
def validate_amap(key: str) -> tuple[bool, str]:
    """用候选 key 发一个最小请求，确认能用。"""
    from . import amap

    key = (key or "").strip()
    if not key:
        return False, "key 为空"
    previous = os.environ.get("AMAP_KEY")
    os.environ["AMAP_KEY"] = key
    try:
        pois = amap.search_pois("杭州", keywords="景点", page_size=1)
        if pois:
            return True, f"连通正常（返回 {len(pois)} 条）"
        return True, "连通正常（没有返回 POI，但接口可用）"
    except amap.AmapError as exc:
        return False, str(exc)
    finally:
        if previous is None:
            os.environ.pop("AMAP_KEY", None)
        else:
            os.environ["AMAP_KEY"] = previous


def validate_model(name: str) -> tuple[bool, str]:
    """确认模型在本机 Ollama 里存在。"""
    from . import llm

    name = (name or "").strip()
    if not name:
        return False, "模型名为空"
    if not llm.available():
        return False, f"连不上 Ollama（{ollama_host()}）"
    models = llm.list_models()
    if not models:
        return False, "Ollama 里没有已下载的模型"
    if name in models:
        return True, f"已就绪（共 {len(models)} 个可用）"
    return False, f"没有这个模型，可选：{'、'.join(models[:6])}"


def apply(updates: dict, verify: bool = True) -> dict:
    """应用网页上提交的配置。先校验，再落 .env，最后生效到当前进程。"""
    changes, warnings, errors = [], [], []
    to_write = {}

    if "amap_key" in updates:
        key = (updates.get("amap_key") or "").strip()
        if key:
            ok, message = validate_amap(key) if verify else (True, "未校验")
            if not ok:
                errors.append(f"高德 key 校验失败：{message}")
            else:
                to_write["AMAP_KEY"] = key
                changes.append("高德 key 已更新")
                if not verify:
                    warnings.append(message)

    if "model" in updates:
        name = (updates.get("model") or "").strip()
        if name:
            ok, message = validate_model(name) if verify else (True, "未校验")
            if not ok:
                errors.append(f"模型校验失败：{message}")
            else:
                to_write["TRAVEL_MODEL"] = name
                changes.append(f"模型已切换为 {name}")

    if "ollama_host" in updates:
        host = (updates.get("ollama_host") or "").strip().rstrip("/")
        if host:
            to_write["OLLAMA_HOST"] = host
            changes.append(f"Ollama 地址改为 {host}")

    if errors:
        return {"ok": False, "errors": errors, "changes": changes, "settings": current()}

    if to_write:
        write_env(to_write)
    return {"ok": True, "errors": [], "changes": changes, "warnings": warnings,
            "settings": current()}
