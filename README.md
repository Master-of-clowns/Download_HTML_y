# Выгрузка выдачи в HTML

Пакет `sbs_capture` выгружает картинки и видео в HTML с локальным сохранением или публикацией в S3. Код разделён на модули, а CSS и JavaScript находятся в `sbs_capture/resources`.

**[Инструкция по установке и запуску со своим JSON](docs/SBS_CAPTURE_RU.md)** — скачивание проекта, зависимости, локальная проверка, настройки S3 и получение ссылок.

После установки зависимостей из `requirements-sbs.txt` локальная проверка запускается так:

```bash
python -m sbs_capture --input examples/sbs-queries.json --output runs-local --local-only
```

Прежняя команда `python sbs_capture_single.py ...` поддерживается. Для неё нужна соседняя папка `sbs_capture` со всеми модулями и ресурсами.

## Запуск существующего парсера видео run.py

Один раз установи зависимости:

```bash
python3 -m pip install -r requirements.txt
```

Установите переменные среды с ключами корзины:
```bash
read -rs 'AWS_ACCESS_KEY_ID?Введи Access Key ID: '; echo
export AWS_ACCESS_KEY_ID

read -rs 'AWS_SECRET_ACCESS_KEY?Введи Secret Access Key: '; echo
export AWS_SECRET_ACCESS_KEY

export S3_BUCKET='bac33a93-fc8a-445a-b967-41d71601c7a5'
export S3_ENDPOINT_URL='https://s3.twcstorage.ru'
export AWS_DEFAULT_REGION='ru-1'
export S3_PREFIX='yandex-video'
export S3_URL_MODE='presigned'
export S3_URL_TTL_SECONDS='86400'
```

Или укажи ключи и параметры S3 в файле `.env` рядом с `run.py`. Настройки парсера заданы в коде.

Сам запуск:

```bash
python3 run.py input.json
```


При первом запуске Chromium установится автоматически. Результат сохранится в `result.json`.

Чтобы задать имя выходного файла:

```bash
python3 run.py input.json output.json
```

Вместо `input.json` укажи путь к своему файлу. Пути с пробелами заключай в кавычки.

