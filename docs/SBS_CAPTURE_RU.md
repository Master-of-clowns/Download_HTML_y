# Установка и запуск выгрузки картинок в HTML

`sbs_capture` сохраняет выдачу Яндекс Картинок в HTML для каждого запроса из вашего JSON. Можно сохранить страницы на ноутбуке или загрузить их в S3 и получить ссылки. Весь проект нужно держать в одной папке: короткий файл `sbs_capture_single.py` запускает модули из папки `sbs_capture`.

## Скачать проект

На странице GitHub с этой версией проекта нажмите **Code → Download ZIP**, распакуйте архив и откройте терминал в распакованной папке. В ней должны быть `sbs_capture`, `requirements-sbs.txt` и `examples/sbs-queries.json`.

Если используете Git, после принятия pull request скачайте основной репозиторий:

```bash
git clone https://github.com/Master-of-clowns/Download_HTML_y.git
cd Download_HTML_y
```

Для проверки до принятия pull request используйте его исходную копию:

```bash
git clone https://github.com/semionkiselev5-spec/Download_HTML_y.git
cd Download_HTML_y
```

## Установить Python и зависимости

Нужен Python 3.10 или новее. Если Python ещё не установлен, скачайте установщик с [python.org](https://www.python.org/downloads/). В Windows при установке включите **Add Python to PATH**.

На macOS или Linux из папки проекта выполните:

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-sbs.txt
```

В Windows откройте **cmd** в папке проекта и выполните:

```bat
py --version
py -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -r requirements-sbs.txt
```

При новом открытии терминала достаточно перейти в папку проекта и снова активировать `.venv`. Далее команды `python ...` одинаковы для всех систем.

## Подготовить свой JSON

Создайте файл, например `my-queries.json`:

```json
[
  {"query": "горный пейзаж", "region_id": 213, "device": "DESKTOP"},
  {"query": "закрытый купальник полностью", "region_id": 10739, "device": "IPHONE"}
]
```

У каждого запроса обязательны три поля:

| Поле | Значение |
| --- | --- |
| `query` | Непустая строка с поисковым запросом |
| `region_id` | Числовой идентификатор региона поиска |
| `device` | `DESKTOP`, `ANDROID` или `IPHONE` |

Поддерживаются также один JSON-объект и JSONL: по одному объекту в строке. Сохраните файл в UTF-8. Готовый небольшой пример находится в `examples/sbs-queries.json`.

## Проверить локально без S3

```bash
python -m sbs_capture --input examples/sbs-queries.json --output runs-local --local-only
```

Для своего файла замените только путь после `--input`:

```bash
python -m sbs_capture --input "my-queries.json" --output runs-local --local-only
```

Можно указать полный путь к JSON в другой папке. Пути с пробелами заключайте в кавычки. `--local-only` обязателен для запуска без S3: без него программа пытается публиковать страницы.

После запуска откройте папку `runs-local`, затем подпапку с идентификатором запуска. В ней будут HTML каждого запроса и `results.json`. Откройте HTML в браузере. Для загрузки картинок, исходных стилей и просмотрщика требуется интернет.

Статус `"ok"` и поле `html_path` означают, что HTML сохранён на ноутбуке. В этом режиме публичных ссылок нет. Если статус `"error"`, причина указана в поле `error`; обработка остальных запросов продолжается.

## Загрузить HTML в S3

В `config.images.standard.json` задайте свои `s3.endpoint`, `s3.region`, `s3.bucket` и `s3.prefix`. Текущие значения относятся к исходной настройке проекта. Настройки `acl` и `public_base_url`, если он задан, должны соответствовать вашему хранилищу: программа возвращает публичные ссылки и проверяет их чтение без авторизации.

Ключи S3 передаются через окружение или стандартный AWS-профиль. Для временного ввода ключей из терминала откройте Python:

```bash
python
```

Вставьте этот блок в приглашение `>>>` и нажмите Enter. Ключи вводятся скрыто по запросу; после завершения программа вернётся в терминал:

```python
exec('import os, sys, getpass\nos.environ["AWS_ACCESS_KEY_ID"] = getpass.getpass("Access Key ID: ")\nos.environ["AWS_SECRET_ACCESS_KEY"] = getpass.getpass("Secret Access Key: ")\nos.execv(sys.executable, [sys.executable, "-m", "sbs_capture", "--input", "my-queries.json", "--config", "config.images.standard.json", "--output", "runs-s3"])')
```

Перед вставкой замените `my-queries.json` на путь к своему файлу. Этот способ работает с активированной `.venv` на macOS, Linux и Windows и не сохраняет ключи в файле проекта.

Если ключи уже настроены в окружении либо доступен AWS-профиль, запустите напрямую:

```bash
python -m sbs_capture --input "my-queries.json" --config config.images.standard.json --output runs-s3
```

При успешной публикации ссылка печатается строкой `HTML: https://...`, записывается в поле `s3_url` и в `urls.txt` внутри папки запуска. Если локальный HTML сохранён, но публикация не прошла, результат содержит `"status": "error"` и объяснение ошибки. `html_path` сам по себе не означает, что файл загружен в S3.

## Настройки выдачи и отсутствие блюра

Без `--config` используются настройки из `sbs_capture/resources/default_config.json`. С `--config` загружается указанный JSON целиком; используйте `config.images.standard.json` как образец полного конфига.

По умолчанию для `DESKTOP` программа собирает не менее 90 картинок, используя до пяти страниц; последнюю страницу сохраняет целиком. Если выдача закончилась раньше, фактическое количество отражается в метаданных. Мобильная выдача сохраняется с одной страницы.

Блюр отключается при каждой выгрузке на всех устройствах, включая просмотрщик и миниатюры видео. Значение `remove_blur` принудительно устанавливается в `true`, даже если в конфиге записано `false`. Для картинок сохраняются исходные ссылки на ресурсы: браузер загружает их при открытии HTML, а выгрузка не скачивает все оригиналы отдельно.

## Запуск видео и прежней команды

Для режима видео дополнительно установите браузер:

```bash
python -m playwright install chromium
python -m sbs_capture --input "my-queries.json" --service video --output runs-video --local-only
```

Команда с прежним именем файла запускает тот же пакет и принимает те же параметры:

```bash
python sbs_capture_single.py --input "my-queries.json" --config config.images.standard.json --output runs-local --local-only
```

Существующий `run.py` — отдельный парсер видео со своим форматом входа и зависимостями. Его запуск описан в [README](../README.md).

## Где находятся части кода

| Файл или папка | Назначение |
| --- | --- |
| `sbs_capture/cli.py` | Команды запуска, обработка JSON, отчёты |
| `sbs_capture/models.py` | Запросы и записи выдачи |
| `sbs_capture/urls.py` | Адреса поиска и источников |
| `sbs_capture/capture.py` | Получение выдачи |
| `sbs_capture/parser.py` | Разбор карточек картинок и видео |
| `sbs_capture/pagination.py` | Дополнительные страницы desktop-выдачи |
| `sbs_capture/render.py` | Сохранение HTML и отключение блюра |
| `sbs_capture/assets.py` | Загрузка ресурсов для видео и обработка CSS |
| `sbs_capture/storage.py` | Публикация в S3 и проверка ссылки |
| `sbs_capture/templates.py` | Чтение CSS и JavaScript из отдельных файлов |
| `sbs_capture/resources/` | CSS, JavaScript и настройки по умолчанию |
| `requirements-sbs.txt` | Зависимости этого пакета |
| `tests/` | Локальные проверки без реальной публикации в S3 |

Для проверки кода:

```bash
python -m pip install -r requirements-dev-sbs.txt
python -m pytest tests
```

Коды завершения программы: `0` — все запросы успешны, `1` — есть ошибки отдельных запросов, `2` — ошибка настройки или запуска.
