"""Ollama 调用层：纯标准库 HTTP，不引第三方依赖。

放在单独模块的原因：方便测试时替换（monkeypatch），也让"模型不可用就降级"只有一处判断。
"""
import json
import os
import urllib.error
import urllib.request

def default_host() -> str:
    from . import settings

    return settings.ollama_host()


def default_model() -> str:
    from . import settings

    return settings.model()


# 兼容旧引用：值在调用时仍以 settings 为准
DEFAULT_HOST = default_host()
DEFAULT_MODEL = default_model()

# 本机 Ollama 不能走系统代理：Windows 上 urllib 会读注册表里的代理设置，
# 一旦代理软件开着"系统代理"，127.0.0.1 的请求会被转发出去并拿到 502。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LLMError(RuntimeError):
    """模型不可用或返回异常。"""


def available(host: str | None = None, timeout: float = 2.0) -> bool:
    try:
        with _OPENER.open((host or default_host()) + "/api/tags", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def list_models(host: str | None = None, timeout: float = 3.0) -> list[str]:
    """列出本机 Ollama 已下载的模型（设置页的下拉框用它）。"""
    try:
        with _OPENER.open((host or default_host()) + "/api/tags", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return []
    models = []
    for item in payload.get("models") or []:
        name = str(item.get("name") or "").strip()
        if name:
            models.append(name)
    return sorted(models)


def chat(prompt: str, model: str | None = None, host: str | None = None,
         timeout: float = 240, json_mode: bool = True, temperature: float = 0.2) -> str:
    host = (host or default_host()).rstrip("/")
    payload = {
        "model": model or default_model(),
        "prompt": prompt,
        "stream": False,
        # 关掉 qwen3 的思考链：小模型带思考更容易把 few-shot 抄出来，而且更慢
        "think": False,
        "options": {"temperature": temperature, "num_ctx": 8192},
    }
    if json_mode:
        payload["format"] = "json"

    try:
        body = _post(payload, host, timeout)
    except urllib.error.HTTPError:
        # 个别模型不认识 think 参数，去掉再试一次
        payload.pop("think", None)
        try:
            body = _post(payload, host, timeout)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"Ollama 调用失败：{exc}") from exc

    text = body.get("response", "")
    if not isinstance(text, str) or not text.strip():
        raise LLMError("Ollama 返回了空内容")
    return text


def _post(payload: dict, host: str, timeout: float) -> dict:
    request = urllib.request.Request(
        host + "/api/generate",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMError(f"连不上 Ollama（{host}）：{exc}") from exc
    except (ValueError, KeyError) as exc:
        raise LLMError(f"Ollama 返回无法解析：{exc}") from exc
