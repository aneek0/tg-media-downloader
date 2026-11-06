# ADR 0002: Музыка по Spotify-ссылкам через mornye как библиотеку

**Статус:** Accepted
**Дата:** 2026-10-19

## Контекст

Бот отдаёт медиа по ссылкам (yt-dlp, gallery-dl, direct CDN). Пользователям нужна
музыка по Spotify-ссылкам в lossless. Внешний инструмент —
[mornye Module](https://github.com/mornyeapp/Mornye-Python)
(PyPI `mornye`): резолвит метаданные Spotify, качает FLAC через
расширения-провайдеры (Deezer, Tidal, Qobuz), ничего не bundled — реестр
расширений выбирает сам владелец бота.

Рассматривались два способа интеграции. Спавн CLI-субпроцесса отвергнут:
метаданные нужны **до** скачивания (инлайн-статья, клавиатура), spawn + startup
накладка на каждый клик синхронного CLI неудобна; либа `AsyncMornye` даёт
нативный async поверх общего HTTP-пула. Решение по измерениям: import ~0.35s,
метаданные альбома 0.25–0.5s внутри процесса.

## Решение

1. **Интеграция — как библиотека.** Один долгоживущий `AsyncMornye` клиент
   в `services/mornye.py`, `__aenter__` один раз за сессию бота. Скачивание —
   через `download_track` / `download_tracks`; расширения и реестр — в
   `~/.mornye` владельца (MORNYE_REGISTRIES env, как у CLI).

2. **Метаданные одиночного трека — через album-endpoint, не `get_track_async`.**
   В исследованной версии mornye путь `getTrack` GraphQL возвращает вырожденный
   `Unknown`-запись для **всех** track-id (stale persisted-query hash) — silent
   fallback в `TRACK_NOT_FOUND`-фильтре. Рабочий обход (проверен, ~0.9s):
   HTTP GET страницы `open.spotify.com/track/<id>` → `og:title` + `/album/<id>`
   → `get_album_tracks_async(album_id)` → match по track id. Побочно og-404
   служит проверкой валидности ссылки. Album/playlist/artist резолв работает
   напрямую через `get_url_async`.

3. **Провайдер по умолчанию — `ext:deezer`.** Проверен end-to-end
   (~90 s/трек, включая Turnstile через headless Chromium — браузерная
   зависимость остаётся у владельца, не в боте). Дефолт-обогащение
   (`enrich_providers`) без `qobuy`-таймаутов: ограничен список, как
   `"deezer", "apple"`. Список провайдеров — env `MORNYE_SERVICES`.

4. **Формат выдачи — FLAC как есть** (`MORNYE_TRANSCODE` пусто → оригинал;
   `mp3` → MP3 320k транскод). Отправка — Send Type `audio` во всех случаях:
   Telegram-клиент проигрывает FLAC через `send_audio`. Метаданные для
   плеера Telegram: `title`/`performer` берутся из TrackMetadata
   (DownloadArtifact.audio_title/audio_performer), duration — из
   `DownloadArtifact.duration`, иначе hachoir
   (`services/media.py:audio_duration`). Обложка — embedded cover FLAC/MP3
   через mutagen (`services/delivery.py:_extract_cover`,
   временный JPEG, удаляется после отправки); без embedded-обложки
   используется per-user thumbnail владельца.

5. **Доставка коллекций — обложка + audio media-group'ы.** Перед группой
    треков уходит фото обложки (как у deezload2bot): альбом берётся
    `reply_media_group` чанками по 10 (лимит Telegram), каждый элемент —
    `InputMediaAudio` с title/performer/embedded-cover. Одиночный трек —
    одиночный `reply_audio` без отдельного фото.

6. **Терпимость к частичной неудаче.** mornye возвращает per-track
   видимость успехов/провалов; засчитываем то, что скачалось, сообщаем список
   провалов, реп Roverа доготавливает после всех попыток (retry).

7. **Кап коллекции — 50 треков** (конвенция `services/gallery.py:MAX_GALLERY_PHOTOS`),
   с явным сообщением «скачано N из M» (в отличие от тихой отсечки галереи).

8. **Инлайн-режим.** Оба входа: track и album/playlist (обрезанными капом).
   Метаданные для inline-статьи — og-meta (`track`) или `get_url_async`
   (коллекции), без скачивания. Общий cooldown охватывает и инлайн-запросы.

9. **Cache — только одиночные треки.** Album/playlist не кэшируются в Media
   Cache в фазе 1 (Высок выгрыв: 50 file_id на плейлист — редкий re-request,
   добавленная сложность в `media_cache.py` не оправдана).

10. **Прямые ссылки Deezer.** deezer.com/track|album|playlist распознаются
    тем же интейком (`is_mornye_url`, заменяет `is_spotify_url`): резолв —
    `ExtensionMetadataClient.get_url_async` (расширение `ext:deezer` даёт
    имя, треки, обложку), скачивание — тем же DownloadWorker через
    `ext:deezer`. Spotify-метаданные не участвуют.

11. **Сетевой guard расширений.** Node-runtime mornye блокирует
    «приватные» адреса; за fakeip-VPN (sing-box TUN) всё внешнее для
    расширений выглядит как 198.18.x.x и блокируется, а резолвер даёт
    заглушечные 503 от api.zarz.moe. Обход —
    `MORNYE_EXT_ALLOW_PRIVATE_NETWORK=1`. Резолвер `/dl/dzr` ещё и
    дёргучий (ConnectError/503 ~1 из 3) — `track_max_retries=2` в
    DownloadOptions.

12. **Turnstile-верификация (zarz-v2).** Скачиванию нужна подпись
    signed-session; если сессии нет, pydoll поднимает Chromium —
    обязательны `CHROME_PATH` (обёртка helium) и флаги
    `HELIUM_USER_FLAGS` (--no-sandbox и т.п.) в окружении процесса бота.
    Автосолв занимает ~40 c, а внутри bridge-вызова расширения лимит 60 c —
    поэтому провал аутентификации из-за отсутствующего браузера ставит
    файловый backoff (`signed_sessions/.*.auth-backoff.json`, 5 мин × 3 за
    провал, до 4 ч): бот падает «verification paused», даже когда браузер
    уже починили. Лечение — устранить причину, затем удалить backoff-файл
    (gateway он не спрашивал: провал был локальным) и перезапустить.

## Риски и последствия

- **Upstream-дефект** single-track metadata задокументирован и обойдён; при
  апгрейде mornye нужно перепроверить, не починили ли `getTrack`, и убрать
  обход (см. тест на постоянный сбой).
- Скачивание зависит от внешних расширений и Turnstile — медленно и
  нестабильно; вся обработка во время деградации — существующий исключающий
  бок (`executor`/`inline_flow`).
- mornye — библиотека тяжелее субпроцесса: одна зависимость (`mornye`
  в pyproject). Опция отказа встроена в саму конфигурацию: владелец не задаёт
  `MORNYE_SERVICES` — бот не распознаёт Spotify-ссылки, код не выполняется.
