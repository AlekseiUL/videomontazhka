# История изменений

Формат следует Keep a Changelog; версия продукта использует Semantic Versioning.

## [0.1.0] — 2026-08-29

### Добавлено

- Private beta плагина **Видеомонтажка** для Codex на macOS/Apple Silicon.
- Инвентаризация исходников и hash-bound source manifest.
- Transcript-led смысловой монтаж с доказательствами и таймкодами.
- Четыре approval gate: стоимость/приватность, смысл/формат, визуал/звук и preview.
- Exact-file transcription approval, external one-shot anchors, приватный hash-chain ledger, source-byte snapshots, fail-closed retry и безопасный partial resume.
- EDL renderer, локальная графика, звук, provenance, boundary QA и release QA.
- Публикационный пакет для YouTube и коротких форматов.
- Marketplace manifest, документация, синтетический пример и offline CI.
- Прозрачная карта происхождения, лицензий и невендоренных движков.

### Исправлено

- Команды skill используют доступный на чистом macOS `python3`; регрессию блокирует portability-тест.
