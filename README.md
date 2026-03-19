# CloudSafe Helpdesk

Полнофункциональная тикет-система на Flask + SQLite с ролями, статусами, архивом задач и API для создания тикетов по Bearer token.

## Возможности

- Роли: `admin`, `operator`, `client`
- Управление пользователями (админ): создание, редактирование, деактивация
- Управление статусами (админ): создание, редактирование, удаление (если нет задач)
- Активные задачи: список, фильтры, детальный просмотр
- История смены статусов (кто и когда изменил)
- Архив задач (без удаления из БД), восстановление
- Канбан-доска с drag-and-drop (для admin/operator)
- API: `POST /api/tasks` с Bearer token
- Пароли хранятся в виде хеша
- Автосоздание начальных данных при первом запуске

## Стек

- Python
- Flask
- Flask-Login
- Flask-SQLAlchemy (легко переключить SQLite на PostgreSQL через `DATABASE_URL`)
- Bootstrap 5
- SortableJS (для канбана)

## Структура проекта

```text
.
├── app.py
├── api.py
├── auth.py
├── config.py
├── forms.py
├── models.py
├── requirements.txt
├── README.md
├── templates/
│   ├── base.html
│   ├── login.html
│   ├── change_password.html
│   ├── index.html
│   ├── task_form.html
│   ├── task.html
│   ├── users.html
│   ├── user_form.html
│   ├── statuses.html
│   ├── archive.html
│   ├── kanban.html
│   └── error.html
└── static/
    ├── css/
    │   └── style.css
    └── js/
        └── kanban.js
```

## Установка и запуск

1. (Опционально) создать виртуальное окружение:

```bash
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
```

2. Установить зависимости:

```bash
pip install -r requirements.txt
```

3. Запустить приложение:

```bash
python app.py
```

4. Открыть в браузере:

- http://localhost:5000

## Учётные данные по умолчанию

При первом запуске создаются:

- Администратор:
  - логин: `admin`
  - пароль: `admin`
- Статусы:
  - `Новая`
  - `В работе`
  - `Завершена`

Рекомендуется сразу сменить пароль администратора.

## Переменные окружения

- `SECRET_KEY` — секрет Flask
- `DATABASE_URL` — строка подключения к БД (по умолчанию `sqlite:///ticket_system.db`)
- `ADMIN_LOGIN` — логин initial admin (по умолчанию `admin`)
- `ADMIN_PASSWORD` — пароль initial admin (по умолчанию `admin`)

Пример:

```bash
export SECRET_KEY="super-secret-key"
export ADMIN_PASSWORD="strong-password"
python app.py
```

## Работа с API

### Создание токена для клиента

1. Войти как админ
2. Перейти в раздел **Пользователи**
3. Для клиента нажать **Сгенерировать**
4. Скопировать токен (он показывается один раз)

### Endpoint

- `POST /api/tasks`
- Авторизация: `Authorization: Bearer <token>`

### Пример запроса (токен клиента)

```bash
curl -X POST http://localhost:5000/api/tasks \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "theme": "Проблема с VPN",
    "content": "Не подключается VPN с домашнего компьютера",
    "due_date": "2026-04-01"
  }'
```

### Пример запроса (токен admin/operator)

```bash
curl -X POST http://localhost:5000/api/tasks \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "theme": "Новый запрос",
    "content": "Описание задачи",
    "client_id": 3,
    "due_date": "2026-04-05"
  }'
```

Также можно передавать `client_email` или `client_username` вместо `client_id`.

### Пример успешного ответа

```json
{
  "id": 12,
  "theme": "Проблема с VPN",
  "content": "Не подключается VPN с домашнего компьютера",
  "due_date": "2026-04-01",
  "archived": false,
  "created_at": "2026-03-19T19:20:11.123456",
  "status": {
    "id": 1,
    "name": "Новая"
  },
  "client": {
    "id": 3,
    "username": "client1"
  }
}
```

## Примечания

- Архивные задачи нельзя редактировать и переводить в другие статусы
- Клиент видит только свои задачи
- Оператор и администратор видят все задачи
- Для production рекомендуется запускать через WSGI (`gunicorn`) и reverse proxy (`nginx`)
