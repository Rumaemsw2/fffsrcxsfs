import base64
import json
import os
import re

from dotenv import load_dotenv

load_dotenv()
from openai import AsyncOpenAI

# Любой OpenAI-совместимый API. По умолчанию — бесплатный Google Gemini (AI Studio).
client = AsyncOpenAI(
    api_key=os.environ["AI_API_KEY"],
    base_url=os.getenv("AI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"),
)

MODEL = os.getenv("AI_MODEL", "gemini-3.5-flash")
FAST_MODEL = os.getenv("AI_FAST_MODEL", MODEL)

SYSTEM = """Ты — {bot_name}, живой и естественный участник Telegram-чата.
Разговаривай естественно: замечай контекст, помни собеседников, подхватывай темы,
задавай уместные вопросы и иногда сам начинай разговор.
Не отвечай механически на каждое сообщение. Если ответа не требуется — верни ровно NO_REPLY.
Не говори, что ты ИИ, если тебя прямо не спросили; на прямой вопрос отвечай честно.
Не выдумывай реальный личный опыт.
Пиши без канцелярита и шаблонов. Обычно 1–4 коротких предложения.
Не повторяй одну и ту же мысль. Не начинай каждую реплику с имени человека.
Можно обсуждать игры, мемы, технику, погоду, бытовые темы и темы из чата.
Если человек явно обращается к тебе — отвечай. Если разговор идёт, можешь задать встречный вопрос.
Матерься редко: только если это очень уместно по контексту и настроению. В большинстве ответов обходись без мата и грубой лексики.

Стикеры и картинки (необязательно, используй редко):
- Чтобы кинуть стикер, добавь в конец ответа тег [STICKER:😂] с одним эмодзи по настроению.
  Не чаще чем примерно в одном ответе из пяти. Можно ответить одним стикером без текста.
- Чтобы прислать РЕАЛЬНОЕ фото из интернета (поиск), добавь в конец ответа тег [PHOTO: короткий запрос на английском].
  Обязательно используй, если просят «покажи», «кинь фото», «скинь картинку», «найди фото» чего-то реального
  (животное, машина, еда, место, человек из мема, предмет и т.п.).
  Примеры: [PHOTO: cute cat] [PHOTO: red ferrari] [PHOTO: pizza close up]
- Чтобы СГЕНЕРИРОВАТЬ картинку нейросетью, добавь тег [IMG: подробное описание сцены на английском].
  Только когда явно просят «нарисуй» / «сгенерируй» или нужна фантазия/несуществующее.
- Максимум один тег картинки за ответ (либо PHOTO, либо IMG). Теги не объясняй и не упоминай — они скрыты от людей.
  Можно ответить короткой фразой + тег, например: лови [PHOTO: husky puppy]
Если в сообщении есть фото — посмотри на него и отреагируй по делу, как человек в чате.
Сообщения вида [фото] и [стикер 😂] в истории — это то, что присылали люди."""


def _system(bot_name):
    return SYSTEM.replace("{bot_name}", bot_name)


async def _complete(system, content, model, max_tokens=1500):
    # max_tokens с запасом: у «думающих» моделей рассуждения тоже считаются.
    response = await client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
    )
    return (response.choices[0].message.content or "").strip()


def _with_image(text, image):
    """image = (bytes, media_type) или None."""
    if not image:
        return text
    data, media_type = image
    url = f"data:{media_type};base64,{base64.b64encode(data).decode()}"
    return [
        {"type": "image_url", "image_url": {"url": url}},
        {"type": "text", "text": text},
    ]


async def should_speak(context, incoming, direct=False, bot_name="КПК"):
    if direct:
        return True
    prompt = f"""Реши, стоит ли сейчас естественно участвовать в разговоре.
Отвечай только YES или NO.
YES — если есть вопрос, шутка, эмоция, интересная тема или естественный повод добавить короткую реплику.
NO — если сообщение служебное, слишком личное между людьми, односложное или ответ будет спамом.
Не требуй обязательного обращения к боту.

КОНТЕКСТ:
{context}

ПОСЛЕДНЕЕ СООБЩЕНИЕ:
{incoming}"""
    result = await _complete(_system(bot_name), prompt, FAST_MODEL, max_tokens=300)
    return result.upper().startswith("YES")


async def chat(context, incoming, direct=False, bot_name="КПК", autonomous=False, image=None):
    if autonomous:
        prompt = f"""КОНТЕКСТ ГРУППОВОГО ЧАТА:
{context}

В чате некоторое время тихо. Сам начни короткую реплику только если есть естественный повод.
Можно поздороваться, спросить продолжение темы, вернуться к недавнему обсуждению,
пошутить или поднять лёгкую новую тему. Не притворяйся, что тебя только что спросили.
Если хорошего повода нет — верни ровно NO_REPLY. Если пишешь — 1–3 живых предложения."""
    else:
        prompt = f"""КОНТЕКСТ ГРУППОВОГО ЧАТА:
{context}

ТЕКУЩАЯ СИТУАЦИЯ:
{incoming}

Ответь естественно и по смыслу. Если уместно, добавь короткий встречный вопрос или продолжи тему.
Если отвечать действительно не стоит — верни ровно NO_REPLY."""
    return await _complete(_system(bot_name), _with_image(prompt, image), MODEL)


STICKER_RE = re.compile(r"\[STICKER:\s*([^\]]+?)\s*\]", re.I)
IMG_RE = re.compile(r"\[IMG:\s*([^\]]+?)\s*\]", re.I)
PHOTO_RE = re.compile(r"\[PHOTO:\s*([^\]]+?)\s*\]", re.I)


def parse_reply(raw):
    """-> (текст, [эмодзи стикеров], gen_prompt | None, search_query | None)"""
    raw = (raw or "").strip()
    if not raw or "NO_REPLY" in raw:
        return "", [], None, None
    stickers = [s.strip() for s in STICKER_RE.findall(raw)]
    imgs = IMG_RE.findall(raw)
    photos = PHOTO_RE.findall(raw)
    text = PHOTO_RE.sub("", IMG_RE.sub("", STICKER_RE.sub("", raw))).strip()
    gen_prompt = imgs[0].strip() if imgs else None
    search_query = photos[0].strip() if photos else None
    # если оба — приоритет у реального поиска
    if search_query and gen_prompt:
        gen_prompt = None
    return text, stickers, gen_prompt, search_query


async def extract_facts(context):
    prompt = f"""Извлеки только устойчивые факты, которые помогут в будущих разговорах.
Только явно сказанные факты: имя/как обращаться, хобби, любимые игры, долгосрочные проекты,
устойчивые предпочтения. Не сохраняй пароли, токены, адреса, здоровье, финансы,
политические предпочтения или другие чувствительные данные. Не додумывай.
Верни JSON: [{{"username":"...","fact":"..."}}]. Если ничего нового — [].
Только JSON, без пояснений.

КОНТЕКСТ:
{context}"""
    result = await _complete(
        "Ты аккуратный модуль долговременной памяти. Не выдумывай факты.",
        prompt,
        FAST_MODEL,
        max_tokens=1500,
    )
    try:
        text = result.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except Exception:
        return []
