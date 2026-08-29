# История изменений

Формат следует Keep a Changelog; версия продукта использует Semantic Versioning.

## [0.1.1] — 2026-08-30

### Исправлено

- Gate 3 теперь машинно блокирует производство графики и creative SFX без
  `creative_approval.json`, привязанного SHA-256 к текущему treatment plan.
- Контракт deliverable отделяет строгую минимальную длительность от допуска
  целевой длительности ±15%.
- Doctor проверяет, что настроенный executable — совместимый Python 3.11+, а не
  только существующий файл.
- Базовый runtime публикуется атомарно под bounded install lock; manifest
  сохраняет полный фактически установленный Python inventory с отметкой прямых
  зависимостей.
- Дополнены CI attribution, author/community links и единая формулировка
  `verified default-branch revisions`.

## [0.1.0] — 2026-08-29

### Добавлено

- Public beta плагина **Видеомонтажка** для Codex на macOS/Apple Silicon.
- Инвентаризация исходников и hash-bound source manifest.
- Transcript-led смысловой монтаж с доказательствами и таймкодами.
- Четыре approval gate: стоимость/приватность, смысл/формат, визуал/звук и preview.
- Exact-file transcription approval, external one-shot anchors, приватный hash-chain ledger, source-byte snapshots, fail-closed retry и безопасный partial resume.
- EDL renderer, локальная графика, звук, provenance, boundary QA и release QA.
- Публикационный пакет для YouTube и коротких форматов.
- Marketplace manifest, документация, синтетический пример и offline CI.
- Прозрачная карта происхождения, лицензий и невендоренных движков.

### Исправлено

- Команды skill используют проверенный product runtime на Python 3.11+; регрессию блокирует runtime-path test.
- Инсталлятор останавливается до мутации runtime на Python ниже 3.11, а документация использует явно проверенный интерпретатор вместо предположения о версии системного `python3`.
- CI закрепляет Python 3.12 и проверяет минимальную версию до запуска contract tests.
- Doctor отделяет обязательные FFmpeg-фильтры от условных `subtitles`/`zscale`, поэтому отсутствие burned-subtitle или HDR capability не блокирует базовый SDR/sidecar-контур.
