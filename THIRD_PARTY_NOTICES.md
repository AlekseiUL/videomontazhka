# Сторонние компоненты и уведомления

Этот файл — карта происхождения и лицензий, а не юридическая консультация. Он различает то, что действительно находится в репозитории, и то, что пользователь может установить отдельно. Версия указывается только там, где она зафиксирована включённым lock/requirements-файлом или самим кодом.

## Включено в репозиторий

| Компонент | Источник | Версия/ревизия | Лицензия | Что включено | Изменения |
|---|---|---|---|---|---|
| video-use | <https://github.com/browser-use/video-use> | Единый import commit не был записан; проверенные default-branch revisions четырёх helper-путей перечислены в `PROVENANCE.md`; полнота по всем remote refs пока не подтверждена | MIT, Copyright (c) 2026 Browser Use | Производственные правила, transcript-led подход и четыре помеченных производных файла | Существенно переработано: четыре approval gate, hash-bound артефакты, смысловой и творческий планы, provenance, preview/final QA |
| Unbounded | <https://github.com/google/fonts/tree/main/ofl/unbounded> | Точная версия не заявляется; SHA-256 хранится в font manifest | SIL Open Font License 1.1 | Variable TTF и OFL-текст | Файл шрифта не изменён |
| Golos Text | <https://github.com/google/fonts/tree/main/ofl/golostext> | Точная версия не заявляется; SHA-256 хранится в font manifest | SIL Open Font License 1.1 | Variable TTF и OFL-текст | Файл шрифта не изменён |
| JetBrains Mono | <https://github.com/google/fonts/tree/main/ofl/jetbrainsmono> | Точная версия не заявляется; SHA-256 хранится в font manifest | SIL Open Font License 1.1 | Variable TTF и OFL-текст | Файл шрифта не изменён |

Документированное default-branch сравнение выполнено для файлов `pack_transcripts_safe.py`, `render_edl.py`, `transcribe_batch_safe.py` и `transcribe_safe.py`. Карта, хеши, проверенный scope и оставшийся remote-ref gap находятся в `PROVENANCE.md`. При обнаружении нового производного файла эта карта и его header должны быть обновлены до релиза.

Полный MIT-текст video-use находится в `third_party/licenses/video-use-MIT.txt`. Полные OFL-тексты и оригинальные copyright-строки находятся рядом с соответствующими файлами в `plugins/videomontazhka/skills/videomontazhka/assets/fonts/`. Это место является каноническим: при копировании шрифта в проект инструмент копирует и его лицензию.

## Опционально устанавливается, но не вендорится

| Компонент | Источник | Зафиксированная версия | Лицензия/условия | Политика проекта |
|---|---|---|---|---|
| HyperFrames | <https://github.com/heygen-com/hyperframes> | `0.7.106` в карте движков | Apache-2.0 | Пакет, CLI и `node_modules` не включены. Локальные адаптеры работают только с отдельно установленной проверенной версией. |
| GSAP | <https://gsap.com/licensing/> | `3.14.2` в scaffolder и схемах | Собственная лицензия GSAP; не OSI open source | Бандлы и плагины не включены. Scaffolder требует явный `--accept-gsap-terms`, сохраняет license URL и hashes package metadata. Наличие адаптера не выдаёт лицензию на GSAP. |
| Manim Community | <https://github.com/ManimCommunity/manim> | `0.20.1` в requirements | MIT | Устанавливается в отдельное локальное окружение только по выбранной творческой задаче. |
| PixiJS | <https://github.com/pixijs/pixijs> | `8.19.0` в browser lock | MIT | Не включён; устанавливается по lock-файлу. |
| pixi-filters | <https://github.com/pixijs/filters> | `6.1.5` в browser lock | MIT | Не включён; устанавливается по lock-файлу. |
| Three.js | <https://github.com/mrdoob/three.js> | `0.185.1` в browser lock | MIT | Не включён; устанавливается по lock-файлу. |
| Rough Notation | <https://github.com/rough-stuff/rough-notation> | `0.5.1` в browser lock | MIT | Не включён; устанавливается по lock-файлу. |
| lottie-web | <https://github.com/airbnb/lottie-web> | `5.13.0` в browser lock | MIT | Не включён; устанавливается по lock-файлу. |
| gl-transitions | <https://github.com/gl-transitions/gl-transitions> | `1.71.0` в browser lock | MIT для коллекции | Не включён. Перед публикацией конкретного шейдера требуется отдельная проверка его заголовка/автора и разрешения. |
| FFmpeg | <https://ffmpeg.org/legal.html> | Определяется локальным `ffmpeg -version` | LGPL/GPL и дополнительные условия в зависимости от сборки | Бинарник не включён. Renderer фиксирует путь, хэш и строку конфигурации локальной сборки. Сборки с x264 и другими GPL-компонентами нельзя переименовывать в «MIT-зависимость». |

Lock-файл browser runtime также перечисляет транзитивные npm-пакеты под MIT,
BSD-3-Clause и ISC. Они не находятся в Git-репозитории; установщик сверяет
integrity и лицензионные metadata, а для прямых зависимостей сохраняет найденные
license texts. Это не считается полным комплектом уведомлений для перепоставки
готового `node_modules`: перед такой поставкой необходимо отдельно собрать
лицензии всех фактически включённых транзитивных пакетов.

## Внешние сервисы

ElevenLabs Scribe — внешний API, а не распространяемый компонент. Репозиторий не включает SDK, ключи или купленные минуты. Аудио может быть отправлено сервису только после отдельного подтверждения пользователя. Использование регулируется актуальными условиями и тарифом аккаунта ElevenLabs.

## Инфраструктура репозитория

GitHub Actions использует `actions/checkout` версии `v7.0.1`, закреплённый за
commit `3d3c42e5aac5ba805825da76410c181273ba90b1`. Исходник:
<https://github.com/actions/checkout>; лицензия MIT. Action запускается только в
CI и не включается в дистрибутив Видеомонтажки.

## Python-зависимости

Python-пакеты не вендорятся. Их версии и диапазоны находятся в:

- `plugins/videomontazhka/skills/videomontazhka/requirements.txt`;
- `plugins/videomontazhka/skills/videomontazhka/assets/creative-python-requirements.v1.txt`;
- `plugins/videomontazhka/skills/videomontazhka/assets/manim-runtime-requirements.v1.txt`.

Полный список и лицензионная политика приведены в [DEPENDENCIES.md](DEPENDENCIES.md). При любой поставке готового runtime необходимо заново сформировать SBOM и приложить лицензии именно тех артефактов, которые фактически распространяются.

## Как добавить новую зависимость

В одном изменении должны появиться источник, точная версия или integrity/hash, лицензия, статус bundled/not bundled, отметка об изменениях и тест, который не скачивает произвольный latest. Компонент с неясной лицензией остаётся выключенным до проверки владельцем репозитория.
