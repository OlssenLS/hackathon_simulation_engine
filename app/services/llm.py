import httpx
from typing import Optional
from app.config import settings


async def generate(system: str, user: str) -> str:
    """Provider-agnostic LLM caller using direct REST calls via httpx.

    Supported providers: 'gemini', 'groq'.
    Timeout: 12 seconds, 1 retry.
    """
    api_key = settings.LLM_API_KEY
    if not api_key:
        raise ValueError("LLM_API_KEY is not configured")

    provider = (settings.LLM_PROVIDER or "gemini").lower()
    model = settings.LLM_MODEL or ("gemini-1.5-flash" if provider == "gemini" else "llama-3.1-8b-instant")

    async with httpx.AsyncClient(timeout=12.0) as client:
        last_exc = None
        for attempt in range(2):
            try:
                if provider == "gemini":
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
                    payload = {
                        "systemInstruction": {"parts": [{"text": system}]},
                        "contents": [{"parts": [{"text": user}]}],
                        "generationConfig": {
                            "temperature": 0.4,
                            "maxOutputTokens": 1000,
                        },
                    }
                    resp = await client.post(url, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates and "content" in candidates[0]:
                        parts = candidates[0]["content"].get("parts", [])
                        if parts and "text" in parts[0]:
                            return parts[0]["text"]
                    raise ValueError(f"Unexpected response format from Gemini: {data}")

                elif provider == "groq":
                    url = "https://api.groq.com/openai/v1/chat/completions"
                    headers = {
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    }
                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "temperature": 0.4,
                        "max_tokens": 1000,
                    }
                    resp = await client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    choices = data.get("choices", [])
                    if choices and "message" in choices[0]:
                        return choices[0]["message"].get("content", "")
                    raise ValueError(f"Unexpected response format from Groq: {data}")

                else:
                    raise ValueError(f"Unsupported LLM_PROVIDER: {provider}")

            except Exception as e:
                last_exc = e
                if attempt == 0:
                    continue  # Retry once
                raise last_exc
