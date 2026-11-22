# workbot — личная рабочая база в Telegram

Локальный бот (aiogram 3 + aiosqlite + SQLite FTS5). Без внешних ИИ-API:
индексация и поиск целиком на VPS.

## Структура

```
workbot/
├── main.py            # точка входа: бот, middleware, роутеры, планировщик
├── config.py          # .env -> Settings
├── database.py        # схема, FTS5 + триггеры, items, timings, snapshot для бэкапа
├── extractors.py      # pdf / docx / xlsx / txt -> текст (pypdf, python-docx, openpyxl)
├── middlewares.py     # AdminOnlyMiddleware (только ADMIN_ID)
├── utils.py           # теги, FTS-запрос, пути хранилища, форматирование
├── reports.py         # недельный отчёт по таймингам .xlsx
├── backup.py          # work_base.db + storage/ -> zip -> Telegram
├── scheduler.py       # APScheduler, бэкап раз в сутки в 03:00
├── handlers/
│   ├── __init__.py    # порядок роутеров
│   ├── common.py      # /start /help /backup
│   ├── content.py     # приём текста, фото, документов
│   ├── search.py      # /find + inline-кнопки
│   └── timers.py      # /timer /stats
├── deploy/workbot.service
├── requirements.txt
└── .env.example
```

Файлы и скриншоты: `storage/files/YYYY-MM/`, `storage/screens/YYYY-MM/`.
В БД путь хранится относительно `storage/`, поэтому базу и папку можно переносить целиком.

## Деплой на Ubuntu 22.04 / 24.04

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip

# пользователь и пустая папка (без --create-home: git clone в непустую папку не пройдёт)
sudo useradd --system --no-create-home --home-dir /opt/workbot --shell /usr/sbin/nologin workbot
sudo mkdir -p /opt/workbot /etc/workbot

# deploy key для приватного репозитория (ключ лежит вне папки проекта)
sudo ssh-keygen -t ed25519 -N "" -f /etc/workbot/deploy_key
sudo touch /etc/workbot/known_hosts
sudo chown -R workbot:workbot /opt/workbot /etc/workbot
sudo cat /etc/workbot/deploy_key.pub     # вставить в GitHub: репозиторий -> Settings -> Deploy keys (read-only)

# клонируем код
SSH_CMD='ssh -i /etc/workbot/deploy_key -o UserKnownHostsFile=/etc/workbot/known_hosts -o StrictHostKeyChecking=accept-new'
sudo -u workbot git clone -c core.sshCommand="$SSH_CMD" git@github.com:YOUR_GITHUB_ACCOUNT/YOUR_REPOSITORY.git /opt/workbot

# окружение
sudo -u workbot python3 -m venv /opt/workbot/.venv
sudo -u workbot /opt/workbot/.venv/bin/pip install -r /opt/workbot/requirements.txt

# настройки
sudo -u workbot cp /opt/workbot/.env.example /opt/workbot/.env
sudo -u workbot chmod 600 /opt/workbot/.env
sudo -u workbot nano /opt/workbot/.env      # BOT_TOKEN, ADMIN_ID

# сервис
sudo cp /opt/workbot/deploy/workbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now workbot
journalctl -u workbot -f
```

Обновление кода:

```bash
sudo -u workbot git -C /opt/workbot pull
sudo -u workbot /opt/workbot/.venv/bin/pip install -r /opt/workbot/requirements.txt
sudo systemctl restart workbot
```

`.env`, `work_base.db`, `storage/` и `tmp/` в `.gitignore`, поэтому `git pull` их не трогает.

## Использование

| Что отправить | Что произойдёт |
|---|---|
| Текст | заметка; `#теги` -> колонка tags; `#kb` -> категория kb |
| Фото / скриншот | `storage/screens/`, подпись (caption) индексируется |
| .pdf .docx .xlsx .txt | `storage/files/`, текст извлекается и ищется по содержимому |
| `/find договор оплата` | FTS5-поиск (слова связаны через AND, ищутся по префиксу) |
| `/timer Отчёт` | таймер с кнопками Старт / Пауза / Стоп |
| `/stats` | .xlsx с таймингами за 7 дней (сводка + подробный лист) |
| `/backup` | бэкап немедленно (автоматически — в 03:00) |

## Восстановление из бэкапа

```bash
sudo systemctl stop workbot
cd /opt/workbot && unzip workbot_backup_*.zip     # получите work_base.db и storage/
sudo chown -R workbot:workbot /opt/workbot
sudo systemctl start workbot
```

Если архив пришёл частями (`.zip.001`, `.zip.002`...): `cat workbot_backup_*.zip.* > workbot_backup.zip`.

## Ограничения

- Bot API: бот скачивает файлы до 20 МБ и отправляет до 50 МБ (большие бэкапы режутся на части).
- PDF-сканы без текстового слоя не распознаются (OCR нет) — такой файл ищется только по названию и тегам.
- Поиск префиксный с грубым отсечением русских окончаний (без полноценной морфологии):
  «договорами» найдёт «договор», «договору», «договора», но не «контракт».
  Короткие слова (до 5 букв) ищутся только по началу слова: «база» не найдёт «базы».


## Workbot 2 — обновление

Workbot 2 добавляет удобное меню Telegram, дерево разделов/подразделов, карточки
записей (название, текст, теги, цена, условия, срок действия), редактирование,
удаление с подтверждением, улучшенный поиск, дедлайны с уведомлениями за 24 часа,
за 1 час и в момент наступления срока, OCR скриншотов и повторное распознавание
старых скриншотов `/reindex` (порции до 50).

### Установка

**Не удаляйте старую базу `work_base.db`, `.env` и папку `storage/`**. Перед первым
запуском сделайте полный бэкап базы и скриншотов (`/backup`), остановите сервис.

1. Распакуйте архив обновления. Склонируйте исходный проект, если его нет локально:
   `git clone https://github.com/YOUR_GITHUB_ACCOUNT/YOUR_REPOSITORY.git`
2. Запустите из папки распакованного обновления:
   `python apply_upgrade.py /путь/к/BOT`
   На Windows можно использовать `py apply_upgrade.py C:\\путь\\BOT`.
3. В папке BOT: `python -m pip install -r requirements.txt`.
4. На Ubuntu: `sudo apt install -y tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng`.
   На Windows нужен отдельный Tesseract OCR и языки rus+eng в PATH.
5. Проверьте синтаксис: `python -m compileall -q .` и запустите бота.
6. **После локальной проверки** можно выполнить `git add .`, `git commit -m "Workbot 2"`, `git push`.

На VPS после отправки кода в GitHub обновите рабочую копию: `git pull`, установите
зависимости и перезапустите сервис `sudo systemctl restart workbot`.

Данные остаются в SQLite и существующем `storage`. Новые таблицы создаются автоматически.
Файлы исходного кода, которые меняет установщик, копируются в
`.workbot_upgrade_backup/` (эта папка добавлена в `.gitignore`).

### Пользование

- `/start` или `/menu`: меню с базой знаний, поиском, добавлением, дедлайнами,
  таймерами и отчётом.
- Перешлите скриншот, фотографию, заметку или файл в личный чат с ботом.
  Бот сохранит оригинал и предложит выбрать раздел и заполнить цену/срок.
- Нажмите «База знаний», чтобы просматривать карточки по разделам и создавать
  собственные разделы/подразделы.
- «Поиск»: ищет слова из OCR, названия, тексты документов, теги, цену, раздел,
  описание, даты создания и действия.
- «Дедлайны»: создавайте задачи, переносите сроки, отмечайте выполнение.
- «Таймеры»: запуск, пауза, завершение; «Отчёт» — сохранённая XLSX-статистика.
- `/reindex`: распознаёт до 50 старых скриншотов, которые ранее индексировались
  только по подписи. Команду можно повторить.

### Важные ограничения

OCR работает локально (без облачных сервисов). Распознанные **цены и даты могут
содержать ошибки**, поэтому в карточку вводятся только подтверждённые данные.
Фото должны быть читаемыми; PDF-сканы без текстового слоя пока не распознаются.
Уведомления работают, пока бот запущен; если бот был выключен, напоминание о
прошедшем сроке придёт после следующего запуска. Создание дедлайна требует точного
времени; часовой пояс берётся из `TIMEZONE` в `.env`. Данные не синхронизируются с
другими системами. Резервные копии по прежнему расписанию сохранены.
