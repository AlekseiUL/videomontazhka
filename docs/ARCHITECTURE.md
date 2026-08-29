# Архитектура

## Цели

Архитектура оптимизирует не «количество эффектов», а четыре свойства: исходники не портятся, решения можно проверить, платные действия нельзя запустить случайно, а результат воспроизводим в пределах зафиксированных инструментов.

## Слои

```text
Codex skill
  ├─ policy: вопросы, approvals, смысл и творческие решения
  ├─ contracts: JSON schemas и hash-bound manifests
  ├─ local engine: inventory, pack, EDL, render, QA, provenance
  ├─ optional adapters: HyperFrames, Manim, browser stack, GSAP
  └─ external boundary: ElevenLabs только после Gate 1

user project
  ├─ source media opened read-only by supported product scripts
  └─ edit/ — вся изменяемая история проекта

application data
  └─ ~/Library/Application Support/Videomontazhka/
     ├─ runtime/
     └─ transcription-approvals/  # private one-shot anchors/markers
```

Plugin manifest отвечает за поставку, `SKILL.md` — за поведение агента, scripts — за детерминированные операции, schemas — за формальные контракты, references — за подробные правила, assets — за маленькие проверяемые шаблоны и лицензированные шрифты.

## Разделение control plane и data plane

Codex является control plane: понимает запрос, читает компактные доказательства, предлагает решения и получает approvals. Media tools являются data plane: вычисляют hashes, транскрибируют одобренный объём, режут по EDL, рендерят и проверяют. Скрипт не должен сам придумывать смысл, а prompt не должен выполнять небезопасный media filter без проверки.

## Состояние проекта

Проект движется только вперёд через проверенные переходы:

```text
NEW
 -> INVENTORIED
 -> TRANSCRIPTION_APPROVED
 -> TRANSCRIBED
 -> SEMANTIC_PLAN_PENDING
 -> SEMANTIC_PLAN_APPROVED
 -> CREATIVE_PLAN_PENDING
 -> CREATIVE_PLAN_APPROVED
 -> PREVIEW_RENDERED_AND_QA_PASS
 -> PREVIEW_APPROVED
 -> FINAL_RENDERED_AND_RELEASE_QA_PASS
```

Stale hash возвращает этап в соответствующее pending-состояние. Это не ошибка пользователя; это защита от использования решения для других байтов.

## Граница артефактов

Канонические артефакты живут в `edit/`. Каждый критичный файл содержит версию схемы и/или hash предыдущего этапа. Важны не конкретные имена, а связи:

| Артефакт | На что привязан | Что доказывает |
|---|---|---|
| Source manifest | Байты источников | Какие точные файлы анализировались |
| Transcription preflight/approval | Точные source paths/hashes, provider/model, upload set, предел минут, цитата | Какие конкретные внешние передачи разрешены |
| External attempt marker + project ledger | Approval nonce/ID, canonical project path, preflight/source identity, предыдущая ledger-запись | Что одна сетевая попытка погашена до HTTP и не восстанавливается откатом `edit/` |
| Transcript metadata | Source hash, provider, model | Что кэш относится к этим байтам и ASR identity |
| Packed transcript manifest | Source/transcript hashes | Из каких точных доказательств собран `takes_packed.md` |
| Semantic plan | Evidence/timecodes | Какие исходные факты поддерживают структуру |
| `approval.json` | Plan hash | Какую смысловую версию подтвердил человек |
| EDL | Approval/plan hash | Что cut следует согласованному смыслу |
| Visual provenance | Treatment, source, generator, hashes | Откуда взялся каждый ассет |
| Preview manifest | EDL, renderer, FFmpeg, assets | Как был собран просмотренный файл |
| Preview approval | Preview + manifest hashes | Какой конкретно preview принят |
| Release manifest | Preview approval + final bytes | Что финал не подменил согласованный монтаж |

## Runtime portability

Ни один скрипт не должен зависеть от `/Users/<developer>/...`, другого skill или рабочей копии Studio. `runtime_paths.py` выбирает per-user data path. Переопределения:

- `VIDEOMONTAZHKA_HOME` — весь application home;
- `VIDEOMONTAZHKA_RUNTIME_DIR` — только runtime root;
- `VIDEOMONTAZHKA_CACHE_HOME` — удаляемый cache;
- `VIDEOMONTAZHKA_PYTHON` — проверенный interpreter;
- `VIDEOMONTAZHKA_ENV_FILE` — локальный файл секретов.

Импорт `runtime_paths.py` не создаёт каталогов. Установка происходит только через явный `install_runtime.py --install`, offline-проверка — через `--verify-only`.

## Сеть

Обычные inventory, analysis, EDL, render и QA не нуждаются в сети. Разрешённые сетевые границы:

1. явная установка зафиксированных зависимостей;
2. подтверждённая транскрибация конкретного некэшированного объёма;
3. получение внешнего ассета только после проверки источника и лицензии.

CI не использует ни одну из них после checkout.

## Творческие движки

Router выбирает capability, а registry отвечает, доступна ли проверенная локальная реализация. Базовые FFmpeg/Pillow карты работают без browser runtime. HyperFrames, Manim и browser stack изолированы. GSAP остаётся невендоренным из-за собственной лицензии; наличие adapters не превращает его в зависимость Apache-2.0 продукта.

## Отказоустойчивость

- Новые manifests публикуются через temporary file + atomic replace.
- Existing runtime не перезаписывается установщиком.
- Незавершённый результат имеет `.part`/temporary имя и не считается готовым; восстановить можно только полный response с точной встроенной cache identity.
- Project lock предотвращает параллельный transcript batch; exclusive marker вне проекта погашает capability до HTTP, а append-only hash-chain ledger сохраняет локальную трассировку событий внутри проекта. Это не внешний неизменяемый журнал и не доказательство личности.
- Неоднозначный сетевой исход блокирует автоматический retry и требует нового preflight/approval; успешно завершённые sources возобновляются из кэша.
- Перед извлечением аудио одобренные source bytes копируются в приватный snapshot и повторно сверяются по size/SHA-256; FFmpeg не читает изменяемый pathname после этой проверки.
- Ошибка QA блокирует approval/final, а не превращается в warning.
- Возобновление начинается с проверки hashes и последнего валидного gate, а не с полного повтора.

## Дальнейшее развитие

Перед переходом из private beta нужны: публично воспроизводимый dependency lock с hashes, тестовая матрица версий macOS/FFmpeg и отдельная проверка публичных примеров на права/персональные данные. Исторический commit импорта `video-use` не был записан; вместо выдуманного hash в `PROVENANCE.md` сохранено документированное сравнение всех релевантных датированных upstream-версий. Formal SBOM обязателен, если будущий релиз начнёт поставлять готовые runtimes или binaries.
