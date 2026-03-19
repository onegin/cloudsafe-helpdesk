# CloudSafe Helpdesk

Тикет-система на Flask + SQLite с ролями, организациями, доступами операторов, уведомлениями (Email/Telegram), архивом, API и историей изменений.

## Основные возможности

- Роли: `admin`, `operator`, `client`
- Организации (`Organization`) и сотрудники клиентов внутри организации
- Задачи:
  - обязательная привязка к организации
  - опциональная привязка к конкретному сотруднику
- Настраиваемые статусы задач
- Приоритет, ответственный оператор, комментарии, история изменений
- Архив/восстановление задач
- API: создание задач по Bearer token
- Уведомления: Telegram или Email

## Новое в версии 1.0

### 1. Организации

- Добавлена модель `Organization`:
  - `id`, `name`, `description`, `created_at`, `updated_at`
- Администратор может:
  - создавать, редактировать, удалять организации
  - удалять только если нет связанных пользователей и задач
- Новая страница: **Организации**

### 2. Привязка пользователей к организациям

- В `User` добавлено поле `organization_id`
- Для роли `client` организация обязательна
- Для `admin`/`operator` организация не требуется

### 3. Привязка задач к организациям

- В `Task` добавлено поле `organization_id`
- `client_id` теперь опционален:
  - задача на организацию: `client_id = NULL`
  - задача на сотрудника: `client_id = <user_id>`, при этом сотрудник обязан принадлежать организации задачи

### 4. Доступ операторов

- Старый доступ `operator -> client` заменён на `operator -> organization`
- Новая модель: `OperatorOrganizationAccess`
- Новая страница: **Доступ**

### 5. Логика видимости

- `admin`: видит все задачи
- `operator`: видит задачи организаций, к которым у него есть доступ
- `client`: видит
  - личные задачи (`task.client_id == current_user.id`)
  - общие задачи своей организации (`task.client_id is NULL` и `task.organization_id == current_user.organization_id`)

### 6. Футер на всех страницах

Внизу каждой страницы:

- слева: `ООО «Систем-А» – 2026 год.`
- справа: `CloudSafe HelpDesk v1.0`

## Стек

- Python 3
- Flask
- Flask-Login
- Flask-SQLAlchemy
- SQLite (или PostgreSQL через `DATABASE_URL`)
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
│   ├── index.html
│   ├── task_form.html
│   ├── task.html
│   ├── archive.html
│   ├── kanban.html
│   ├── users.html
│   ├── user_form.html
│   ├── organizations.html
│   ├── organization_form.html
│   ├── access.html
│   ├── statuses.html
│   ├── profile.html
│   ├── login.html
│   ├── change_password.html
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

Основные переменные:

- `SECRET_KEY`
- `DATABASE_URL`
- `ADMIN_LOGIN`
- `ADMIN_PASSWORD`
- `CSRF_ENABLED`
- `APP_VERSION`

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

### POST /api/tasks

Создание задачи по Bearer token.

Заголовки:

- `Authorization: Bearer <TOKEN>`
- `Content-Type: application/json`

### Вариант 1: токен клиента

Тело:

```json
{
  "theme": "Не работает VPN",
  "content": "С утра нет подключения",
  "due_date": "2026-12-31",
  "priority": "high"
}
```

Поведение:

- `organization_id` и `client_id` из запроса не используются для смены владельца
- задача создаётся на самого клиента в его организации

### Вариант 2: токен администратора/оператора

Тело:

```json
{
  "theme": "Обновить сертификаты",
  "content": "Сроки подходят к концу",
  "organization_id": 3,
  "client_id": 12,
  "due_date": "2026-12-31",
  "priority": "medium",
  "assigned_to_id": 5
}
```

`client_id` опционален. Если не передан, задача создаётся как общая для организации.

Вместо `client_id` можно передать:

- `client_email`
- `client_username`

Ограничения:

- оператор может создавать задачи только в доступных ему организациях
- клиент должен принадлежать указанной организации

## Миграция существующей БД

При запуске `python app.py` автоматически выполняется простая миграция:

1. Добавляются новые поля в `users` и `tasks`
2. При необходимости таблица `tasks` перестраивается (чтобы `client_id` стал nullable и появился `organization_id`)
3. Для каждого существующего клиента без организации создаётся организация вида `Компания <username>`
4. Существующие задачи получают `organization_id` (по клиенту задачи)
5. Старые доступы `client_access` конвертируются в `operator_organization_access`

После миграции проверьте:

1. список организаций
2. доступы операторов
3. корректность пользователей-клиентов (организация должна быть заполнена)

## Примечания

- Архивные задачи нельзя редактировать и комментировать
- Клиент видит только личные и общие задачи своей организации
- Для production рекомендуется `gunicorn` + `nginx`
