# Установка private beta

## Поддерживаемая конфигурация

Первая проверяемая цель — macOS на Apple Silicon, актуальный Codex, Git, Python 3.11+ и FFmpeg/ffprobe. Нужен доступ к приватному репозиторию `AlekseiUL/videomontazhka`. ElevenLabs и опциональные визуальные движки для самой установки не требуются.

Проверьте архитектуру и базовые команды:

```bash
uname -m
python3 --version
ffmpeg -version
ffprobe -version
```

Ожидаемая архитектура beta — `arm64`. Если FFmpeg отсутствует, установите его выбранным вами способом; Homebrew-вариант:

```bash
brew install ffmpeg
```

FFmpeg не поставляется проектом. Лицензия локальной сборки зависит от её флагов и включённых кодеков.

## 1. Получить репозиторий

```bash
git clone git@github.com:AlekseiUL/videomontazhka.git
cd videomontazhka
```

Если SSH не настроен, используйте HTTPS только через обычный credential helper GitHub. Не вставляйте personal access token в URL, prompt или shell history.

## 2. Установить плагин в Codex

Предпочтительный путь — добавить локальный marketplace `.agents/plugins/marketplace.json` через интерфейс Codex и установить **Видеомонтажку**. Так Codex видит plugin manifest, skill и будущие обновления одной поставкой.

Совместимый ручной fallback:

```bash
mkdir -p ~/.codex/skills
ln -sfn "$PWD/plugins/videomontazhka/skills/videomontazhka" ~/.codex/skills/videomontazhka
```

После установки начните новую задачу Codex и явно вызовите `$videomontazhka` в первом запросе.

## 3. Создать базовый runtime

Runtime хранится вне Git и вне папки с видео. На macOS путь по умолчанию:

```text
~/Library/Application Support/Videomontazhka/runtime/python
```

Установка является явным сетевым действием: она может обратиться к Python package index, создаёт новое изолированное окружение и при ошибке удаляет только недостроенную новую цель.

```bash
python3 plugins/videomontazhka/skills/videomontazhka/scripts/install_runtime.py --install
```

Скрипт откажется перезаписывать уже существующий runtime. Проверка полностью offline:

```bash
python3 plugins/videomontazhka/skills/videomontazhka/scripts/install_runtime.py --verify-only
```

Чтобы использовать другой корень, задайте `VIDEOMONTAZHKA_HOME` или более узкий `VIDEOMONTAZHKA_RUNTIME_DIR` до установки. Не направляйте runtime внутрь репозитория или проекта с видео.

## 4. Запустить doctor

```bash
python3 plugins/videomontazhka/skills/videomontazhka/scripts/doctor.py
python3 plugins/videomontazhka/skills/videomontazhka/scripts/doctor.py --json
```

Doctor ничего не устанавливает и не вызывает платный API. Он показывает фактические пути, доступные инструменты, необязательные возможности и блокеры. Отсутствие GSAP/Manim/HyperFrames не мешает базовому монтажу: эти движки нужны только для выбранных сцен.

## 5. Настроить транскрибацию при необходимости

Ключ ElevenLabs допускается только в окружении процесса или в продуктовом файле:

```text
~/Library/Application Support/Videomontazhka/.env
```

Создайте его вручную с правами только владельца:

```bash
mkdir -p "$HOME/Library/Application Support/Videomontazhka"
cp .env.example "$HOME/Library/Application Support/Videomontazhka/.env"
chmod 600 "$HOME/Library/Application Support/Videomontazhka/.env"
```

Затем заполните `ELEVENLABS_API_KEY=` локально. При транскрибации агент обязан передать этот точный путь через `--env-file`; скрипт не сканирует домашние папки или другие skills в поиске ключа. Никогда не кладите ключ в `.env` репозитория, папку видео, `project.json`, prompt или сообщение review. Наличие ключа не является согласием потратить лимит: перед каждым новым платным объёмом нужен отдельный preflight approval. Его одноразовые anchors и consumed markers сохраняются рядом с runtime в приватном `transcription-approvals/`; они не содержат ключа, transcript text или media bytes.

Путь продуктового `.env` можно переопределить через `VIDEOMONTAZHKA_ENV_FILE`; выбранный путь всё равно передаётся транскриптору явно.

## 6. Проверить на своей папке

Скопируйте исходники в отдельную рабочую папку, откройте её в Codex и запросите:

> Используй $videomontazhka. Пока только проинвентаризируй папку, покажи режим источников и preflight транскрибации. Ничего платного не запускай.

Ожидаемый безопасный результат — новый `<videos_dir>/edit/`, source manifest и понятный вопрос перед сетью. Исходные файлы не должны измениться.

## Обновление

Сначала сохраните незакоммиченные изменения, затем обновите приватный clone обычным `git pull --ff-only`. Повторно запустите offline-проверку runtime и doctor. Если requirements изменились, старое окружение не переписывается: создайте новое в отдельном явном пути, проверьте его и только затем переключите `VIDEOMONTAZHKA_PYTHON`.

## Удаление

Удаление symlink не удаляет clone или проекты:

```bash
unlink ~/.codex/skills/videomontazhka
```

Runtime и папки `edit/` удаляйте отдельно только после ручной проверки точного пути и резервной копии нужных результатов. Удаление application data также удаляет one-shot anchors: старые approvals после этого безопасно блокируются и потребуют нового preflight/подтверждения. Инсталлятор не выполняет рекурсивное удаление пользовательских данных.
