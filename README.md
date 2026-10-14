# Запуск

Один раз установи зависимости:

```bash
python3 -m pip install -r requirements.txt
```

```bash
python3 run.py input.json
```

При первом запуске Chromium установится автоматически. Результат сохранится в `result.json`.

Чтобы задать имя выходного файла:

```bash
python3 run.py input.json output.json
```

Вместо `input.json` укажи путь к своему файлу. Пути с пробелами заключай в кавычки.


Ключи S3 укажи в `.env` рядом с `run.py`.
