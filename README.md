# Assembler

Внутрішній інструмент motion-команди WF. Робить готові креативи з чистих відео (`_nSub`): титри, тайтли, стікери, трім і виріз, переозвучка, музика під хук, локалізація, а потім збирає хук, боді й пекшот у фінальні ролики пачкою.

Працює локально: сервер FastAPI плюс один HTML-інтерфейс, відео обробляє ffmpeg. Команда отримує `Assembler.app`, який при кожному запуску сам тягне свіжий код з цього репозиторію (healthcheck, автоматичний відкат). Історія змін лежить у [`CHANGELOG.md`](CHANGELOG.md).

## Що всередині

```
engine/
  subtitles.py    титри: розкладка на рядки, karaoke-події, рендер PNG (Pillow)
  compose.py      рендер і збірка: титри → хук → боді → пекшот, трім, виріз посередині, кеш боді
  styles.py       схема стилю титрів, пресети
  textrules.py    правила тексту (числа, одиниці, «не розривати»)
  transcribe.py   Whisper (локально) + ElevenLabs Scribe
  ffmpeg_utils.py ffprobe, кодек під платформу, детект катів
  library.py      бібліотека боді / пекшотів / музики
  dub.py, heygen.py, synclab.py, localize.py   локалізація: дубляж і ліпсинк
  revoice.py      переозвучка зміненого тексту тим самим голосом (ElevenLabs, розпізнавання мовців)
  music11.py      Hook music + SFX (ElevenLabs Video-to-Music / Compose, Audio Isolation)
  bodydetect.py   межа Hook | Body порівнянням кліпів паку
  jira.py         імпорт тасок з Jira + локалізації з ADAM (тільки читання)
server/app.py     HTTP API для інтерфейсу
web/index.html    увесь інтерфейс (Edit · LOC · Assemble)
packaging/        збірка Assembler.app, автооновлення, лаунчер
tests/            офлайн-тести на справжньому відео й звуці (без мережі й кредитів)
tools/            healthcheck для автооновлення, бенчмарк
styles/ fonts/    стилі титрів і шрифти за замовчуванням
caption_rules.json правила титрів за замовчуванням
```

## Запуск (розробка)

Потрібні macOS, Python 3.9+ і ffmpeg (`brew install ffmpeg`).

Найпростіше: двічі клікнути `run.command`. Перший запуск створить `venv` і поставить залежності. Відкриється `http://127.0.0.1:8765`.

Вручну:
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 -m server.app
```

## Ключі і налаштування

Ключі **ніколи** не лежать у репозиторії (він публічний). Кожен дизайнер вводить свої в ⚙ Settings, і вони зберігаються в `~/.assembler/config.json` на його Mac:
- **ElevenLabs:** транскрипція, дубляж, переозвучка, музика. Потрібні дозволи ключа: Speech to Text, Text to Speech, Voices (Write), Dubbing (Write), Music Generation, Audio Isolation.
- **HeyGen / Sync Lab:** ліпсинк.
- **Jira:** особистий email і API token (починається з `ATATT`).

Поза репозиторієм також живуть: **проекти й сесії** `~/.assembler/sessions/` (автозбереження кожні 20 с; незбережені сесії старші за 7 днів і видалені проекти старші за 14 днів прибираються самі), пресети стилів `~/.assembler/styles/`, власні правила титрів, кеш боді `~/.assembler/cache/`, бібліотека (шлях задається в Settings).

## Переїзд на інший Mac

⚙ Settings → **Move to a new Mac** → ⬇ Export my setup: один zip з ключами, шляхом до бібліотеки, пресетами й правилами титрів. На новому Mac: клонувати репо (або поставити `.app`) → ⬆ Import setup → перезапуск. Окремо скопіювати: бібліотеку (якщо вона не на Drive) і, для збірки `.app`, `packaging/preconfig.json`.

## Тести

```bash
python3 tests/test_jira.py        python3 tests/test_cut.py
python3 tests/test_revoice.py     python3 tests/test_hookmusic.py
python3 tests/test_bodydetect.py  python3 tests/test_render_onepass.py
node tests/test_remap.js          node tests/test_revoice_select.js
node tests/test_ui_refs.js        python3 tests/test_transcribe_trim.py
```

## Збірка Assembler.app

```bash
bash packaging/build_app.sh
```
Результат: `dist/Assembler.zip`. Деталі в [`packaging/README_BUILD.md`](packaging/README_BUILD.md). Спільні ключі для `.app` лежать у `packaging/preconfig.json` (ігнорується git).
