import html
import re

# Вырезаем стандартные цветные эмодзи, оставляя строгие технические глифы (•, ▸, ›, ↻, ↶, +, ▪, ─)
EMOJI_STRIP_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002702-\U000027B0"
    "\U0000FE0F"
    "]+",
    flags=re.UNICODE,
)


def strip_emojis(text: str) -> str:
    return EMOJI_STRIP_RE.sub("", text)


def md_to_tg_html(text: str, is_streaming: bool = False) -> str:
    """
    Конвертирует Markdown от LLM в чистый Telegram HTML с кастомным строгим оформлением.
    Корректно обрабатывает даже незакрытые блоки кода во время живого стриминга.
    """
    if not text:
        return ""

    text = strip_emojis(text)

    # Если идет стрим и блок кода ``` еще не закрыт — временно закрываем его
    if is_streaming and text.count("```") % 2 == 1:
        text = text + "\n```"

    code_blocks = []
    inline_codes = []

    # 1. Сохраняем многострочные блоки кода ```lang ... ```
    def _save_code_block(match):
        lang = (match.group(1) or "").strip()
        code = html.escape(match.group(2).strip("\n"))
        if lang:
            block = f'<pre><code class="language-{html.escape(lang)}">{code}</code></pre>'
        else:
            block = f"<pre><code>{code}</code></pre>"
        code_blocks.append(block)
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = re.sub(r"```([a-zA-Z0-9_\-\+#]*)\n?(.*?)```", _save_code_block, text, flags=re.DOTALL)

    # 2. Сохраняем инлайн-код `...`
    def _save_inline_code(match):
        code = html.escape(match.group(1))
        inline_codes.append(f"<code>{code}</code>")
        return f"\x00INLINECODE{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", _save_inline_code, text)

    # 3. Экранируем остальной текст от случайных < > &
    text = html.escape(text)

    # 4. Горизонтальные разделители (--- или ***) -> аккуратная линия
    text = re.sub(r"(?m)^\s*([-\*_]){3,}\s*$", "<code>────────────────────────</code>", text)

    # 5. Заголовки (### Заголовок) -> строгий кастомный маркер ▸ Заголовок
    text = re.sub(r"(?m)^#{1,6}\s+(.+?)\s*$", r"\n<b>▸ \1</b>", text)

    # 6. Цитаты (> текст)
    text = re.sub(r"(?m)^&gt;\s?(.*)$", r"<blockquote>\1</blockquote>", text)
    text = text.replace("</blockquote>\n<blockquote>", "\n")

    # 7. Жирный текст (**текст** или __текст__)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.DOTALL)

    # 8. Курсив (*текст*)
    text = re.sub(r"(?<!\*)\*([^\*\n]+?)\*(?!\*)", r"<i>\1</i>", text)

    # 9. Ссылки [текст](url)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s\)]+)\)", r'<a href="\2">\1</a>', text)

    # 10. Списки (- пункт или * пункт) -> кастомные буллиты •
    text = re.sub(r"(?m)^(\s*)[-\*]\s+", r"\1• ", text)

    # 11. Возвращаем инлайн-код и блоки кода
    for idx, code_html in enumerate(inline_codes):
        text = text.replace(f"\x00INLINECODE{idx}\x00", code_html)

    for idx, block_html in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{idx}\x00", block_html)

    # Убираем лишние пустые строки
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text