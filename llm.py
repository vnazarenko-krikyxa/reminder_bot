"""Будь-яка LLM за ключем: визначаємо провайдера, робимо запит, розпізнаємо голос.

Провайдер визначається за префіксом ключа (gsk_ → Groq, sk-ant- → Anthropic, …). Неоднозначні ключі
(старі «sk-…», ключі без префікса) один раз перевіряємо запитом до /models і запам'ятовуємо результат.
Майже всі провайдери мають OpenAI-сумісний API; Anthropic — через офіційну бібліотеку `anthropic`.
"""
import asyncio
import hashlib
import json
import logging
import re

import httpx

import config
from config import BASE_DIR, LLM_BASE_URL, LLM_MODEL, LLM_PROVIDER

log = logging.getLogger("reminder_bot.llm")

# Без браузерного UA Cloudflare перед деякими API (Groq) відповідає 403
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

# name: base_url (OpenAI-сумісний), моделі для розбору фраз, модель розпізнавання голосу (None — немає)
PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", ["openai/gpt-oss-20b", "openai/gpt-oss-120b"],
             "whisper-large-v3-turbo"),
    "openai": ("https://api.openai.com/v1", ["gpt-4o-mini"], "whisper-1"),
    "anthropic": (None, ["claude-opus-5-5"], None),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", ["gemini-2.5-flash"], None),
    "openrouter": ("https://openrouter.ai/api/v1", ["openai/gpt-4o-mini"], None),
    "xai": ("https://api.x.ai/v1", ["grok-3-mini"], None),
    "deepseek": ("https://api.deepseek.com/v1", ["deepseek-chat"], None),
    "mistral": ("https://api.mistral.ai/v1", ["mistral-small-latest"], None),
}
NAMES = {"groq": "Groq", "openai": "OpenAI", "anthropic": "Anthropic (Claude)", "gemini": "Google Gemini",
         "openrouter": "OpenRouter", "xai": "xAI (Grok)", "deepseek": "DeepSeek", "mistral": "Mistral",
         "custom": "свій сервер"}
# Префікси, що однозначно вказують на провайдера (довші — першими)
PREFIXES = [("sk-ant-", "anthropic"), ("sk-or-", "openrouter"), ("sk-proj-", "openai"), ("gsk_", "groq"),
            ("xai-", "xai"), ("AIza", "gemini")]
CACHE = BASE_DIR / "llm_provider.json"


class LLMError(Exception):
    pass


def guess(key: str) -> tuple[str | None, list[str]]:
    """(провайдер, якщо однозначно; інакше кандидати для перевірки)."""
    key = (key or "").strip()
    for prefix, name in PREFIXES:
        if key.startswith(prefix):
            return name, []
    if key.startswith("sk-"):
        return None, ["openai", "deepseek"]   # старі ключі OpenAI і DeepSeek виглядають однаково
    if re.fullmatch(r"[A-Za-z0-9]{32}", key):
        return None, ["mistral"]
    return None, ["openai", "deepseek", "mistral", "groq"]


def _key_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]  # сам ключ у файл не пишемо


async def _probe(name: str, key: str) -> bool:
    base = PROVIDERS[name][0]
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{base}/models", headers={"Authorization": f"Bearer {key}", "User-Agent": UA})
        return r.status_code == 200
    except httpx.HTTPError:
        return False


async def detect(key: str) -> str | None:
    """Провайдер ключа: за префіксом, з кешу або перевіркою /models у кандидатів."""
    if not key:
        return None
    name, candidates = guess(key)
    if name:
        return name
    kid = _key_id(key)
    try:
        cached = json.loads(CACHE.read_text(encoding="utf-8"))
        if cached.get(kid):
            return cached[kid]
    except (OSError, ValueError):
        cached = {}
    for cand in candidates:
        if await _probe(cand, key):
            cached[kid] = cand
            CACHE.write_text(json.dumps(cached), encoding="utf-8")
            return cand
    return None


def configured() -> bool:
    return bool(config.LLM_API_KEY)


async def provider() -> str | None:
    """Активний провайдер розбору фраз (LLM_PROVIDER / LLM_BASE_URL у .env мають пріоритет)."""
    if LLM_BASE_URL:
        return "custom"
    if LLM_PROVIDER:
        return LLM_PROVIDER
    return await detect(config.LLM_API_KEY)


def _endpoint(name: str):
    if name == "custom":
        return LLM_BASE_URL.rstrip("/"), [LLM_MODEL] if LLM_MODEL else []
    base, models, _ = PROVIDERS[name]
    return base, [LLM_MODEL] if LLM_MODEL else models


def _retry_after(r: httpx.Response) -> float:
    """Секунди до скидання ліміту: retry-after або x-ratelimit-reset-tokens («7.66s», «1m2s»)."""
    if r.headers.get("retry-after"):
        try:
            return float(r.headers["retry-after"])
        except ValueError:
            pass
    m = re.fullmatch(r"(?:(\d+)m)?([\d.]+)s", r.headers.get("x-ratelimit-reset-tokens", ""))
    return int(m.group(1) or 0) * 60 + float(m.group(2)) if m else 5.0


async def chat_json(prompt: str):
    """Відповіді моделей на prompt (текст, де має бути JSON) — по черзі для кожної моделі провайдера.

    Генератор: якщо першу відповідь не вдалося розібрати, викликач бере наступну модель.
    """
    name = await provider()
    if not name:
        raise LLMError("не вдалося визначити провайдера за ключем — вкажи LLM_PROVIDER у .env")
    if name == "anthropic":
        yield await _anthropic(prompt)
        return
    base, models = _endpoint(name)
    if not models:
        raise LLMError("для свого сервера вкажи LLM_MODEL у .env")
    headers = {"Authorization": f"Bearer {config.LLM_API_KEY}", "User-Agent": UA}
    async with httpx.AsyncClient(timeout=60) as client:
        for model in models:
            body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0, "max_tokens": 1500, "response_format": {"type": "json_object"}}
            if "gpt-oss" in model:
                body["reasoning_effort"] = "low"  # інакше міркування може з'їсти весь бюджет
            for attempt in range(2):
                r = await client.post(f"{base}/chat/completions", headers=headers, json=body)
                if r.status_code == 429 and attempt == 0:
                    await asyncio.sleep(min(_retry_after(r), 20))
                    continue
                if r.status_code == 400 and "response_format" in r.text and "response_format" in body:
                    body.pop("response_format")  # не всі моделі підтримують JSON-режим — просимо промптом
                    continue
                break
            if r.status_code != 200:
                log.error("LLM %s/%s: HTTP %s %s", name, model, r.status_code, r.text[:300])
                if r.status_code in (401, 403) or (r.status_code == 400 and "api key" in r.text.lower()):
                    raise LLMError(f"ключ {NAMES.get(name, name)} не підходить — заміни його командою /key")
                if r.status_code == 404 and LLM_MODEL:
                    raise LLMError(f"модель «{LLM_MODEL}» не знайдена в {NAMES.get(name, name)} — перевір LLM_MODEL")
                continue
            yield r.json()["choices"][0]["message"].get("content") or ""


async def _anthropic(prompt: str) -> str:
    import anthropic  # тільки якщо обрано Anthropic

    client = anthropic.AsyncAnthropic(api_key=config.LLM_API_KEY)
    try:
        resp = await client.messages.create(
            model=LLM_MODEL or PROVIDERS["anthropic"][1][0],
            max_tokens=4000,
            output_config={"effort": "low"},  # проста задача розбору — мінімум міркувань
            messages=[{"role": "user", "content": prompt + "\n\nВідповідай лише JSON, без пояснень."}],
        )
    except anthropic.AuthenticationError:
        raise LLMError("ключ Anthropic не підходить")
    except anthropic.RateLimitError:
        raise LLMError("ліміт запитів Anthropic — спробуй за хвилину")
    except anthropic.APIStatusError as e:
        log.error("Anthropic %s: %s", e.status_code, e.message)
        raise LLMError(f"Anthropic відповів {e.status_code}")
    except anthropic.APIConnectionError:
        raise LLMError("немає з'єднання з Anthropic")
    if resp.stop_reason == "refusal":
        raise LLMError("модель відмовилась обробити запит")
    return "".join(b.text for b in resp.content if b.type == "text")


def extract_json(text: str):
    """JSON з відповіді: прибираємо ```-обгортку й зайвий текст навколо."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("у відповіді немає JSON")
    return json.loads(text[start:end + 1])


# ---------- голос ----------

async def _stt_target():
    """(base_url, ключ, модель) для розпізнавання голосу або None."""
    name = await provider()
    if name in PROVIDERS and PROVIDERS[name][2] and name != "custom":
        return PROVIDERS[name][0], config.LLM_API_KEY, PROVIDERS[name][2]
    if config.STT_API_KEY:
        stt = await detect(config.STT_API_KEY)
        if stt in PROVIDERS and PROVIDERS[stt][2]:
            return PROVIDERS[stt][0], config.STT_API_KEY, PROVIDERS[stt][2]
    return None


async def voice_available() -> bool:
    return await _stt_target() is not None


async def transcribe(audio: bytes, filename: str) -> str:
    target = await _stt_target()
    if not target:
        raise LLMError("твій провайдер не розпізнає голос — надішли ключ Groq для голосу командою /voicekey (безкоштовно)")
    base, key, model = target
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{base}/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}", "User-Agent": UA},
            files={"file": (filename, audio)},
            data={"model": model, "response_format": "json", "temperature": "0"},
        )
    if r.status_code != 200:
        log.error("STT %s: %s", r.status_code, r.text[:300])
        raise LLMError("не вдалося розпізнати голос")
    return r.json().get("text", "").strip()


# ---------- ключ із чату (/start, /key) ----------

KEY_RE = re.compile(r"^(gsk_|sk-|AIza|xai-)[A-Za-z0-9_\-]{16,}$|^[A-Za-z0-9]{32}$")


def looks_like_key(text: str) -> bool:
    return bool(KEY_RE.match((text or "").strip()))


async def check_key(key: str) -> tuple[str | None, str | None]:
    """(провайдер, помилка): визначаємо провайдера й перевіряємо ключ справжнім запитом."""
    key = key.strip()
    name = await detect(key)
    if not name:
        return None, "не вдалося визначити, від якої LLM цей ключ"
    if name == "anthropic":
        import anthropic
        try:
            await anthropic.AsyncAnthropic(api_key=key).models.list()
        except anthropic.AuthenticationError:
            return name, "ключ Anthropic не підходить"
        except anthropic.APIConnectionError:
            return name, "немає з'єднання з Anthropic — спробуй ще раз"
        except anthropic.APIStatusError as e:
            return name, f"Anthropic відповів {e.status_code}"
        return name, None
    if not await _probe(name, key):
        return name, f"ключ {NAMES.get(name, name)} не підходить (або сервіс недоступний)"
    return name, None


def has_voice(name: str | None) -> bool:
    return bool(name in PROVIDERS and PROVIDERS[name][2])


def save_key(var: str, key: str) -> None:
    """Записує ключ у .env поруч із ботом і одразу застосовує (без перезапуску)."""
    env = BASE_DIR / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    drop = {var} | ({"GROQ_API_KEY"} if var == "LLM_API_KEY" else set())  # стара назва більше не потрібна
    lines = [ln for ln in lines if ln.split("=", 1)[0].strip() not in drop]
    lines.append(f"{var}={key.strip()}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        env.chmod(0o600)
    except OSError:
        pass
    setattr(config, var, key.strip())
