---
name: videomontazhka
description: Turn a long stream, several takes, or mixed footage into a shorter coherent video through local transcript-led editing. Use for video transcription, semantic selection, filler and dead-air removal, timeline assembly, captions, motion graphics, presenter/screen layouts, audio polish, previews, release QA, YouTube chapters, titles, descriptions, hashtags, and tags. Enforces explicit approval before any paid transcription, before semantic cuts, before visual/audio production, and before final export.
---

# Видеомонтажка

Собирай законченный ролик вокруг реального смысла исходников. Пользователь утверждает расходы, смысл, визуальную подачу и финальный preview; агент автоматизирует всё остальное и сохраняет проверяемое происхождение каждого решения.

## Главные правила

1. Никогда не изменяй исходники. Все артефакты храни только в `<videos_dir>/edit/`.
2. Не отправляй медиа во внешний сервис без явного подтверждения пользователя, привязанного к актуальному manifest и лимиту минут.
3. Не создавай EDL, клипы, графику или render до утверждения точного смыслового плана.
4. Не генерируй полный набор визуалов и звука до утверждения творческой карты или показанных вариантов.
5. Не выпускай final до явного утверждения preview и успешного release QA.
6. Не додумывай факты, реплики, доказательства или вывод. Любое сохранённое утверждение должно иметь source ID и точный timecode.
7. Минимальная длительность, заданная пользователем, — жёсткое ограничение. Не сокращай её молча ради темпа или «виральности».
8. Не трать токены на анализ, который не изменит решение. Сначала используй локальные manifest, hashes, `ffprobe`, схемы и уже созданные артефакты.
9. Не устанавливай зависимости и не вызывай платные API «на всякий случай». Сначала покажи необходимость, объём, альтернативу и спроси.
10. Никогда не печатай ключи, содержимое `.env`, cookies или токены в логах и ответах.

## Когда задавать вопрос

Задай один короткий вопрос и останови зависящую от него ветку, если ответ влияет хотя бы на одно из следующего:

- внешний upload, деньги или privacy;
- смысл, порядок тезисов, обещание зрителю или допустимость спорного удаления;
- формат, обязательный минимум/максимум длительности или набор deliverables;
- новый визуальный стиль, музыка, синтетическая речь/изображение или сторонние материалы;
- действие, которое нельзя безопасно отменить.

Не спрашивай то, что можно достоверно получить из файлов. Объединяй связанные решения в один компактный запрос. Если пользователь уже дал точное ограничение, не переспрашивай.

## Подготовка

Определи абсолютный путь текущего skill как `SKILL_DIR`; не полагайся на текущую рабочую папку. Перед первым проектом:

1. Прочитай [editorial-workflow.md](references/editorial-workflow.md) и [project-format.md](references/project-format.md).
2. Для графики прочитай [visual-language.md](references/visual-language.md). Для retention/creative-задачи также прочитай [creative-direction.md](references/creative-direction.md) и [free-toolchain.md](references/free-toolchain.md).
3. Запусти `python "$SKILL_DIR/scripts/doctor.py"`. Required failure блокирует соответствующую стадию; optional failure только отключает capability.
4. Запусти `python "$SKILL_DIR/scripts/init_project.py" <videos_dir>`. Добавь `--recursive`, если исходники лежат во вложенных папках.
5. Из manifest определи режим:
   - `long_stream` — длинная запись или поток записей с исходной хронологией;
   - `multi_take` — дубли, которые можно переставлять по смыслу;
   - `mixed` — основная запись плюс демонстрации, B-roll или альтернативные дубли.

Повторный запуск должен переиспользовать неизменившиеся артефакты по hash. Если изменился источник, считай зависимые approvals устаревшими и остановись у ближайшего gate.

## Gate 1 — стоимость и приватность транскрибации

До сетевого запроса собери preflight: список аудиодорожек, SHA-256, длительность, уже закэшированные результаты, provider, model, язык и максимум новых billable minutes. Не переводить минуты в деньги по встроенной или предположительной цене; тариф аккаунта пользователь сверяет отдельно.

В beta 0.1 реализован один сетевой provider: ElevenLabs Scribe. Не называй
локальный ASR готовым, пока `doctor.py` и конкретный adapter не подтвердят его.
При отсутствии approval остановись; не выбирай другой облачный сервис молча.

Покажи пользователю для каждого source абсолютный путь, source ID, SHA-256,
длительность, `cached`, `will_upload` и `outside_project`; затем покажи число
новых минут, что останется локально, и попроси точное подтверждение. Зафиксируй
его через `record_transcription_approval.py`. `transcribe_batch_safe.py` обязан
отказать при отсутствии, несовпадении или превышении approval.

Approval разрешает не более одной HTTP-попытки на каждый одобренный source.
Перед сетью writer атомарно погашает external per-user capability, затем
добавляет `attempt_started` в append-only `transcription_attempts.jsonl` с
SHA-256 hash chain. После timeout,
нечитаемого ответа или иной неоднозначности никогда не повторяй запрос сам:
получи новый preflight и новое явное подтверждение. Успешные hash-bound кэши
переиспользуй, а под тем же approval продолжай только sources, для которых
попытка ещё не начиналась.

Не переноси платный approval между application home, хостами или другими
canonical project paths. Если внешний approval anchor отсутствует, создай новый
preflight и запроси новое явное согласие. Перед FFmpeg/upload используй только
private snapshot, чьи фактические size/SHA-256 совпали с approved source.

```bash
python "$SKILL_DIR/scripts/transcription_preflight.py" <videos_dir> --language <code>
python "$SKILL_DIR/scripts/record_transcription_approval.py" \
  --edit-dir <videos_dir>/edit \
  --max-billable-minutes <утверждённый-лимит> \
  --quote '<дословное подтверждение пользователя>' \
  --acknowledge-upload
python "$SKILL_DIR/scripts/transcribe_batch_safe.py" <videos_dir> --language <code>
```

Требования к транскриптам:

- word-level timestamps, язык, source hash и manifest ID;
- сохранение слов-паразитов и hesitation events, если provider их возвращает;
- один writer lock на проект, повторяемость и resume;
- exact upload binding и одна network attempt на approval/source;
- no-audio источники отмечаются как `visual_only`, а не получают выдуманный пустой текст;
- API key берётся только из `ELEVENLABS_API_KEY` либо явно указанного пользователем env-файла и никогда не копируется в проект.

После транскрибации создай evidence view:

```bash
python "$SKILL_DIR/scripts/pack_transcripts_safe.py" --edit-dir <videos_dir>/edit
```

## Gate 2 — смысл, формат и длительность

До любого монтажного действия покажи plain-language план:

- обещание зрителю в одном предложении;
- аудитория и полезный результат;
- реально присутствующие смыслы с evidence ID, source file, modality и точными timecodes;
- 2–4 варианта hook и рекомендация;
- предлагаемый порядок разделов и причина перестановок;
- что оставить, сократить, удалить или перенести;
- слабые места и пробелы, которые нельзя честно исправить исходниками;
- 2–3 разумных варианта длительности/формата, если пользователь не задал их сам;
- отдельный scope, hook и ending для каждого deliverable;
- краткая визуальная и звуковая гипотеза.

Запиши тот же план в `edit/semantic_plan.json` со `status: "pending"`. Заверши прямым запросом подтверждения. Утверждение цвета, отдельной сцены или прошлой версии не является утверждением смыслового плана.

После точного согласия:

```bash
python "$SKILL_DIR/scripts/record_approval.py" \
  --plan <videos_dir>/edit/semantic_plan.json \
  --quote '<дословное подтверждение пользователя>'
python "$SKILL_DIR/scripts/validate_gate.py" --edit-dir <videos_dir>/edit --phase asset
```

Изменение смыслов, порядка, обещания, формата или target duration меняет hash и требует нового approval.

## Смысловой монтаж

Составь EDL только из утверждённых evidence ranges.

- Убирай «э-э», «м-м», ложные старты, дубли, технические паузы и повторы, не несущие нового смысла.
- Сохраняй намеренную паузу, дыхание, эмоцию, шутку, оговорку и доказательство; для длинной паузы укажи `intentional_pause_reason`.
- Для речи используй `audio_mode: source`; для немого B-roll — `audio_mode: mute`.
- Не скрывай оставшийся filler внутри длинного cut: EDL quote должен совпадать с пережившими cut transcript words.
- Не синтезируй замену речи без отдельного разрешения.
- В `multi_take` выбирай дубль по полноте смысла и качеству подачи, а не по имени файла.
- Каждый retained claim сохраняет evidence IDs и точный source envelope.

Перед preview запусти
`validate_gate.py --edit-dir <videos_dir>/edit --phase render --edl
<videos_dir>/edit/edl_<deliverable>.json`; canonical renderer сам обязан
повторить gate с тем же EDL.

## Gate 3 — визуал и звук

Сначала составь маршрутизированную творческую карту по каждому утверждённому beat. Для каждого элемента выбери ровно одно основное решение и при необходимости один звуковой акцент:

- `none` или чистый hard cut;
- presenter/screen layout;
- caption или kinetic text;
- title, chapter, definition, comparison, process, quote или CTA card;
- diagram/route/data animation;
- virtual camera/punch-in;
- local B-roll или user-owned asset;
- короткий approved transition, только если renderer и boundary QA его поддерживают.

Решение `none` нормально. Не украшай каждый тезис. Защищай читаемую область интерфейса и выбирай presenter geometry по исходнику: rectangle по умолчанию для осмысленного кадра, circle только по запросу или если источник уже так устроен.

Проверь доступные capability через `creative_tool_registry.py --json`, затем создай решения `creative_tool_router.py route` и скомпилируй их `compile_creative_treatment_plan.py`. Недоступный или experimental engine нельзя объявлять готовым.

До массовой генерации покажи:

- карту сцен, видимый текст и protected regions;
- 2–4 ключевых still/mockup либо 3–4-frame sheet для нового эффекта;
- вариант A/B, если выбор звука или движения субъективен;
- список внешних assets с лицензией и provenance.

Получив точное визуальное/звуковое подтверждение, создавай только утверждённые assets. Любой новый текст на экране должен совпадать с approved plan. Для каждого asset запиши provenance sidecar и approval hash.

Используй локальные, проверяемые пути:

- `render_motion_card.py` — типографика и data-driven cards;
- `scaffold_creative_browser_effect.py` — открытые локальные Pixi/Three/Rough Notation/Lottie adapters;
- `detect_shots.py`, `plan_virtual_camera.py`, `render_virtual_camera.py` — ненавязчивая виртуальная камера;
- `analyze_rhythm.py`, `generate_creative_sfx.py`, `audio_polish.py` — локальные аудио-кандидаты и обработка;
- Manim — формальные технические диаграммы;
- HyperFrames или GSAP — только как явно установленный optional runtime с принятыми upstream terms; их bundles не входят в этот репозиторий.

Любой scaffolder, копирующий GSAP bytes в проект, должен получить
`--accept-gsap-terms` только после того, как пользователь увидел license URL из
локального `gsap/package.json` и явно согласился. Наличие пакета само по себе не
является согласием.

Не вызывай TTS, stock, music, avatar, image/video generation или другой платный API по умолчанию. Один лишь доступный инструмент не является причиной его использовать.

## Preview, Gate 4 и final

1. Проверь schemas, approval hashes, source hashes, exact boundaries, caption fit, safe zones, overlay provenance, audio peaks/loudness и отсутствие случайного source audio.
2. Отрендери review preview, явно помеченный как preview.
3. Покажи путь к файлу, длительность, разрешение и краткий QA report. Попроси пользователя посмотреть ролик целиком и подтвердить именно эту версию.
4. Запиши утверждение через `record_preview_approval.py`.
5. Запусти `validate_gate.py --edit-dir <videos_dir>/edit --phase final
   --edl <videos_dir>/edit/edl_<deliverable>.json`, затем final render и
   `qa_release.py` для namespaced final render manifest.

Если после preview изменились EDL, asset, audio, subtitle или render config, preview approval становится недействительным.

## Пакет публикации

После успешного final QA подготовь без изменения видео:

- 3 SEO-понятных, но честных названия;
- полное описание с обещанием и полезным итогом;
- главы/таймкоды из final timeline;
- короткий текст обложки без дублирования длинного title;
- видимые hashtags;
- отдельную строку upload tags через запятую до 500 символов;
- `.srt`/`.vtt`, если субтитры утверждены;
- checksum и release report.

Не обещай «виральность» как гарантированный результат и не добавляй ключевые слова, которых нет в ролике.

## Результат и отчётность

Ожидаемая структура проекта:

```text
<videos_dir>/
├── исходники (неизменны)
└── edit/
    ├── project.json
    ├── source_manifest.json
    ├── transcription_preflight.json
    ├── transcription_approval.json
    ├── transcription_attempts.jsonl
    ├── transcripts/
    ├── takes_packed.md
    ├── takes_packed_manifest.json
    ├── semantic_plan.json
    ├── approval.json
    ├── creative/
    ├── edl_<deliverable>.json
    ├── <deliverable-preview>.mp4
    ├── render_manifest_<artifact-key>_preview.json
    ├── preview_approval_<artifact-key>.json
    ├── <deliverable-final>.mp4
    └── release_manifest_<artifact-key>.json
```

В каждом обновлении пользователю указывай текущую фазу, созданные файлы, следующий gate и блокирующие ограничения. Не называй preview финалом и не называй отключённую возможность установленной.
