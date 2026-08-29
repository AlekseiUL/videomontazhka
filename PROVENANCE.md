# Происхождение и цепочка доверия

## Источник этого репозитория

Репозиторий выделен 29 августа 2026 года из внутренней рабочей цепочки SPRUT Video Studio в самостоятельный устанавливаемый продукт. В перенос включались только код, схемы, документация, тесты, небольшие шаблоны и лицензированные шрифты. Пользовательские проекты, исходные записи, транскрипты, EDL, рендеры, ключи, `.env`, виртуальные среды, `node_modules` и общий runtime не переносились.

Это дата выделения продукта, а не утверждение о дате создания каждого исходного файла.

## Upstream video-use

Transcript-led идея, часть производственных правил и часть реализации происходят от проекта [browser-use/video-use](https://github.com/browser-use/video-use), MIT License, Copyright (c) 2026 Browser Use.

Историческая локальная копия, с которой началась разработка, не сохранила единый upstream commit импорта. Этот документ намеренно не придумывает hash. 29 августа 2026 года выполнено файл-к-файлу сравнение четырёх помеченных производных файлов с историей соответствующих helper-путей в default branch `main` (18 commits). Дополнительные remote refs, включая `studio-mac` и перенесённые `src/video_use/*` реализации, требуют отдельной проверки до заявления о полном сравнении всех upstream revisions. Лицензия MIT и Copyright (c) 2026 Browser Use сохранены в `NOTICE` и `third_party/licenses/video-use-MIT.txt`.

### Документированная derivation map

| Upstream path | Локальный производный файл | Проверенные revisions | Наиболее поздняя совместимая revision | Evidence |
|---|---|---|---|---|
| `helpers/pack_transcripts.py` | `pack_transcripts_safe.py` | `fee55aaf32ef9bda551031ea54d36534bca54c31`, `196d7e9377d7265ae61bd9e6189a4b12f33913c1` | `196d7e9377d7265ae61bd9e6189a4b12f33913c1` | В локальном коде сохранена явная запись UTF-8, добавленная этой revision; upstream SHA-256: `f9e419def5f0a014d5e1fd16fdad801013ae068854c1d474c3492297e2304f4b`. |
| `helpers/render.py` | `render_edl.py` | `fee55aaf32ef9bda551031ea54d36534bca54c31`, `44f784714eadc1613d02d9506dfe4c7c772f6e0d`, `60798b1cd5cd4563875aeb06bb9252983084831a`, `87f00c6b9b4199dadf3c3b7a75bf818f3df0695e`, `1200463f00ae53fa562dc8ca2b5f8b7ec7d43f43` | `1200463f00ae53fa562dc8ca2b5f8b7ec7d43f43` | Локальный renderer сохраняет upstream HDR transfer set/tonemap chain и orientation-preserving поведение; 71 точная non-comment строка последней версии остаётся в существенно переработанном файле. Upstream SHA-256: `bef2d6b47659c1d734b47556403276d05f0585e72d4b2d1da159c22b4cad69ed`. |
| `helpers/transcribe_batch.py` | `transcribe_batch_safe.py` | `fee55aaf32ef9bda551031ea54d36534bca54c31` | `fee55aaf32ef9bda551031ea54d36534bca54c31` | Это единственная upstream-версия файла; upstream SHA-256: `6bb73c6f64ff1885d9962ececbe771041a2638b5cc78397c3ef14358671795c3`. |
| `helpers/transcribe.py` | `transcribe_safe.py` | `fee55aaf32ef9bda551031ea54d36534bca54c31` | `fee55aaf32ef9bda551031ea54d36534bca54c31` | Это единственная upstream-версия файла; сохранены функции `load_api_key`, `extract_audio`, `call_scribe` и `main`. Upstream SHA-256: `5be26cbdd56b7e683eb794f92da4dc101aabb8875b130251f191fcab8cf631c7`. |

Сравнение подтверждает перечисленную default-branch карту без ложного утверждения о едином import commit; полнота по всем remote refs пока не подтверждена. Все четыре локальных файла сохраняют явные MIT-derived headers. Полный поиск дерева не обнаружил других файлов с прямыми `video-use` notices или существенными точными блоками upstream-реализации. В `transcription_preflight.py` совпадают только общие поля API Scribe (`language`, `num_speakers`, `timestamps_granularity`), но не алгоритм или связный блок helper-кода; поэтому отдельный derived header там не добавляется. При новом evidence карта должна быть расширена.

## Граница поставки private beta

Правообладатель оригинальной части проекта: **Алексей Ульянов**. В текущую поставку входят исходный репозиторий, документация, тесты, схемы, небольшие шаблоны, лицензированные шрифты и автоматический установщик. Готовые Python/Node runtimes, binaries, `node_modules`, пользовательские media и generated project/source packs не поставляются. Если эта граница изменится, до релиза требуется новый artifact-specific SBOM и комплект лицензий/NOTICE для фактических байтов.

Основные изменения относительно исходной идеи:

- четыре явных approval gate вместо одного общего подтверждения;
- preflight стоимости/приватности до платной транскрибации;
- hash-bound source manifest, transcript cache, plan, EDL, preview и render provenance;
- доказательный смысловой план с цитатами и исходными таймкодами;
- отдельная маршрутизированная творческая карта;
- provenance для внешних ассетов и локальных визуальных движков;
- preview approval, boundary QA и release QA;
- русский продуктовый интерфейс и публикационный пакет.

## Что считается доказательством проекта

Каждый запуск строит цепочку:

```text
source bytes
  -> source SHA-256 manifest
  -> provider/model-bound transcript
  -> evidence-bound semantic plan
  -> hash-bound approval artifact with the exact user quote
  -> plan-bound EDL and creative assets
  -> renderer/tool identity + preview manifest
  -> preview approval
  -> final render manifest + release QA
```

Если меняются исходные байты, одобренный план, EDL, визуальный ассет, код renderer или FFmpeg, прежнее подтверждение не переносится автоматически. Повторное использование допустимо только при совпадении контрактных hashes.

## Внешние ассеты

Для каждого внешнего изображения, видео, музыкального трека, SFX, шаблона или шейдера проект должен сохранять:

- URL или идентификатор источника;
- автора/правообладателя, если указан;
- лицензию и дату получения;
- локальный SHA-256;
- разрешённое назначение;
- отметку об изменениях.

«Можно скачать» не означает «можно публиковать». При отсутствии ясной лицензии ассет не используется.

## Как проверять будущие импорты

Новый upstream-импорт оформляется отдельным коммитом. В описании фиксируются repository URL, tag/commit, пути, лицензия, исходные notices, список модифицированных файлов и команда проверки. Если hash неизвестен, импорт блокируется, а не маркируется приблизительной версией.
