# Диагностика

Начинайте с offline-команд:

```bash
python3 plugins/videomontazhka/skills/videomontazhka/scripts/install_runtime.py --verify-only
python3 plugins/videomontazhka/skills/videomontazhka/scripts/doctor.py --json
```

Не переустанавливайте всё автоматически: это стирает полезные признаки причины и может скачать ненужные пакеты.

## `runtime Python is missing`

Runtime ещё не создан или выбран другой корень. Проверьте `VIDEOMONTAZHKA_HOME`, `VIDEOMONTAZHKA_RUNTIME_DIR` и `VIDEOMONTAZHKA_PYTHON`. Если это первая установка, запустите явный `install_runtime.py --install`. Если окружение существует в другом месте, сначала проверьте его и задайте точный путь, не копируйте `.venv` из другого проекта.

## `runtime already exists and was left untouched`

Это защитное поведение. `--install` не обновляет окружение на месте. Запустите `--verify-only`. Если requirements действительно изменились, создайте новое окружение через `--runtime` в новом точном каталоге, проверьте его и переключите конфигурацию. Старый runtime удаляйте только после проверки нового.

## FFmpeg/ffprobe отсутствует

Установите системный FFmpeg и повторите doctor. Если команд несколько, убедитесь, что Codex видит тот же `PATH`, что терминал. Для provenance важен конкретный binary, поэтому не подменяйте его между preview и final.

## Doctor показывает путь другого пользователя/Studio/video-use

Это portability bug. Не создавайте symlink на чужую рабочую копию. Найдите абсолютную ссылку командой:

```bash
rg -n '/Users/|\.codex/skills/.*/\.venv' plugins/videomontazhka
```

Исправление должно использовать `runtime_paths.py` или skill-relative path и получить regression test.

## Ключ ElevenLabs не найден

Проверьте только наличие строки в process environment или явно указанном продуктовом `.env`; не печатайте значение. Рекомендуемый путь — `~/Library/Application Support/Videomontazhka/.env`, права `0600`, при вызове нужен соответствующий `--env-file`. После исправления снова запустите preflight. Не запускайте batch напрямую в обход Gate 1.

## Approval транскрибации stale или превышен лимит

Source hash, список файлов, модель или некэшированная длительность отличаются от подтверждённого preflight. Это нормальная остановка. Покажите новую дельту пользователю и получите новое подтверждение; не редактируйте approval вручную.

## Транскрибация уже оплатилась, но результата нет

Не повторяйте команду сразу. Проверьте `transcription_attempts.jsonl`, внешний one-shot marker, lock, metadata и целостность ответа. Полный `.part.json` с корректной встроенной cache identity восстанавливается локально; произвольный или неполный `.part` кэшем не является. Если исход внешнего запроса неизвестен, capability уже считается израсходованной: сначала сверяйтесь с кабинетом провайдера, затем создайте новый preflight и получите новое явное подтверждение.

## `semantic approval` или EDL stale

Изменился план либо evidence. Перегенерируйте только зависимый артефакт и верните Gate 2. Не заменяйте hash в JSON вручную: это уничтожает доказательство согласования.

## Опциональный visual engine недоступен

Router должен выбрать локальный fallback или объяснить конкретную сцену, для которой нужен движок. Отсутствие HyperFrames, GSAP, Manim, Pixi или Three не является причиной ставить всё сразу. GSAP пользователь предоставляет отдельно и использует по собственной лицензии.

## Preview проходит, final блокируется

Сравните renderer/FFmpeg identity, plan/EDL/assets hashes и preview approval. Частая причина — обновлённый FFmpeg, изменённый asset или новый код renderer между этапами. Безопасный путь — повторный preview и Gate 4, а не отключение проверки.

## Чёрный кадр, обрезанный текст или щелчок

Сначала откройте boundary QA и контрольные кадры. Проверьте точную frame boundary, alpha/pixel format, font availability, safe margins, subtitle fit и audio fade. Исправьте минимальный слой, создайте новую preview revision и снова пройдите QA.

## Мало места на диске

Рендер требует места для временных mezzanine-файлов, а безопасная транскрибация — для private snapshot крупнейшего source и извлечённого WAV. Очистите только идентифицированный disposable cache или старые отклонённые preview после резервной копии. Не удаляйте source manifest, approvals и provenance вместе с temporary files. После аварийного завершения проверьте точный принадлежащий пользователю `sprut-scribe-*` каталог в системной temporary-папке; не применяйте broad recursive delete.

## Тесты пытаются обратиться к сети

Это дефект. Unit/CI tests должны использовать fixtures/mocks и пустой `ELEVENLABS_API_KEY`. Installer test проверяет команды и manifests, но не устанавливает пакеты. Зафиксируйте failing test и заблокируйте merge до устранения.
