async def generate_gemini_text(prompt_text: str) -> str:
    url = "https://generativelanguage.googleapis.com/v1beta/interactions"
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json",
        "Api-Revision": "2026-05-20",
    }
    payload = {
        "model": GEMINI_MODEL,
        # В документации нет примера системной инструкции для REST,
        # поэтому надёжнее передавать промпт прямо во входном тексте.
        "input": f"{SYSTEM_PROMPT}\n\nИсходные данные:\n{prompt_text}",
    }
    # Необязательно: ускорить генерацию, уменьшив «размышления» модели.
    # Задайте GEMINI_THINKING_LEVEL=low (или minimal) в переменных окружения.
    level = os.getenv("GEMINI_THINKING_LEVEL")
    if level:
        payload["generation_config"] = {"thinking_level": level}

    session = await get_session()
    last_error = None
    for attempt in range(2):
        try:
            async with session.post(
                url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=75),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status in (429, 500, 503):
                    last_error = f"API Error {resp.status}: {data}"
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                if resp.status != 200:
                    raise Exception(f"API Error {resp.status}: {data}")
        except asyncio.TimeoutError:
            last_error = "Gemini не ответил за 75 секунд"
            continue

        status = data.get("status")
        if status and status != "completed":
            raise Exception(f"Генерация не завершена (status: {status}): {data.get('error') or data.get('errors')}")

        texts = []
        for step in data.get("steps") or []:
            if step.get("type") == "model_output":
                for block in step.get("content") or []:
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))

        text = "".join(texts).strip()
        if not text:
            raise Exception("Пустой ответ от Gemini. Нажми «Перегенерировать».")
        return clean_model_output(text)

    raise Exception(last_error or "Не удалось получить ответ от Gemini")
