# CloudSafe Helpdesk

Тикет-система на Flask + SQLite с ролями, разграничением доступа операторов к клиентам, уведомлениями (Email/Telegram), архивом, API и расширенным трекингом изменений.

## Основные возможности

- Роли: `admin`, `operator`, `client`
- Управление пользователями (администратор)
- Настраиваемые статусы задач
- Архив и восстановление задач
- API: создание задач по Bearer token

## Что добавлено в этой версии

### 1. Доступ операторов к задачам клиентов

- Добавлена модель связи `ClientAccess` (`operator_id`, `client_id`)
- Администратор управляет правами на странице **Доступы**
- Оператор видит/редактирует только задачи клиентов, к которым у него есть доступ
- В формах создания/редактирования задачи оператору доступен только его список клиентов
- Проверки доступа выполняются во всех view (включая карточку задачи, архив, канбан, смену статуса)

### 2. Уведомления о новых задачах и комментариях

- У каждого пользователя есть:
  - `email` (обязателен)
  - `telegram_chat_id` (опционально)
- Канал уведомления:
  - если указан `telegram_chat_id` и включён Telegram bot -> Telegram
  - иначе -> Email
- При создании задачи уведомляются:
  - администраторы
  - клиент
  - назначенный ответственный оператор (если есть)
- При комментариях уведомляются:
  - администраторы всегда
  - клиент (если комментарий от админа/оператора)
  - операторы с доступом к клиенту (если комментарий от клиента/оператора)
- Поддерживаются задачи, созданные через Web и API

### 3. Дополнительные функции трекера

Реализовано более 3 функций из списка:

- Комментарии к задачам
- Назначение ответственного оператора
- История изменений полей задачи (`TaskHistory`)
- Приоритет задач (`low/medium/high`)
- Расширенные фильтры и поиск по теме/содержимому

### 4. Безопасность

- Добавлена CSRF-защита для web-форм
- Проверка ролей и прав доступа на уровне view
- Пароли хешируются

## Стек

- Python 3
- Flask
- Flask-Login
- Flask-SQLAlchemy
- SQLite (переключается на PostgreSQL через `DATABASE_URL`)
- Bootstrap 5
- SortableJS (канбан)

## Структура проекта

```text
.
├── app.py
├── api.py
├── auth.py
├── config.py
├── forms.py
├── models.py
├── notifications.py
├── services.py
├── requirements.txt
├── README.md
├── .env.example
├── templates/
│   ├── base.html
│   ├── login.html
│   ├── change_password.html
│   ├── profile.html
│   ├── index.html
│   ├── task_form.html
│   ├── task.html
│   ├── archive.html
│   ├── kanban.html
│   ├── users.html
│   ├── user_form.html
│   ├── access.html
│   ├── statuses.html
│   └── error.html
└── static/
    ├── css/style.css
    └── js/kanban.js
```

## Установка и запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Открыть: `http://localhost:5000`

## Учётные данные по умолчанию

Создаются автоматически при первом запуске:

- Логин: `admin`
- Пароль: `admin`

Рекомендуется сразу сменить пароль.

## Конфигурация

Используйте `.env.example` как шаблон.

Обязательные/часто используемые переменные:

- `SECRET_KEY`
- `DATABASE_URL`
- `ADMIN_LOGIN`
- `ADMIN_PASSWORD`
- `CSRF_ENABLED`

Telegram:

- `TELEGRAM_BOT_ENABLED=true|false`
- `TELEGRAM_BOT_TOKEN=...`

Email (SMTP):

- `MAIL_SERVER`
- `MAIL_PORT`
- `MAIL_USE_TLS`
- `MAIL_USERNAME`
- `MAIL_PASSWORD`
- `MAIL_DEFAULT_SENDER`

## API

### Создание задачи

`POST /api/tasks`

Заголовки:

- `Authorization: Bearer <TOKEN>`
- `Content-Type: application/json`

Тело (пример):

```json
{
  "theme": "Проблема с VPN",
  "content": "Не подключается VPN",
  "client_id": 3,
  "due_date": "2026-12-31",
  "priority": "high",
  "assigned_to_id": 5
}
```

Можно передать `client_email` или `client_username` вместо `client_id`.

## Обновление существующей установки

Если у вас уже есть старая БД `ticket_system.db`, отдельный Alembic не требуется.
При запуске `python app.py` автоматически выполняются простые миграции:

- добавляются новые поля в `users` (`email`, `telegram_chat_id`)
- добавляются новые поля в `tasks` (`priority`, `assigned_to_id`)
- создаются новые таблицы (`client_access`, `task_comments`, `task_history`)

После запуска проверьте в UI:

1. заполнены email пользователей
2. назначены доступы операторов к клиентам (страница **Доступы**)
3. настроены SMTP/Telegram переменные окружения

## Примечания

- Архивные задачи нельзя редактировать и комментировать
- Оператор видит только разрешённых клиентов
- Клиент видит только свои задачи
- Для production рекомендуется запуск через `gunicorn` + `nginx`
