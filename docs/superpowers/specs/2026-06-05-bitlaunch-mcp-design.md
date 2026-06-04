# BitLaunch MCP Server — Design

**Date:** 2026-06-05
**Status:** Approved

## Purpose

MCP-сервер для аренды GPU-машин через BitLaunch (хост Vultr) и выполнения на них
обучения. Потребитель — авторесёрч-агент (Claude Code, Hermes, любой MCP-клиент).
Агент должен уметь полный цикл без участия человека: выбрать план → арендовать →
залить код → запустить обучение → следить за прогрессом → забрать результаты →
удалить машину.

## Verified facts (api recon, 2026-06-05)

- Base URL: `https://app.bitlaunch.io/api`, auth-заголовок: `Authorization: Bearer: <token>`
  (нестандартный формат с двоеточием после Bearer — проверено, работает).
- Хосты: DigitalOcean=0, **Vultr=1**, Linode=2, BitLaunch=4.
- GPU-планы Vultr доступны: 6 планов Nvidia A40 (`vcg-a40-*`), от слайса 2GB VRAM
  (~$0.16/час) до полной A40 48GB (~$3.7/час). Живая доступность ограничена —
  на момент проверки только слайсы 2–8GB VRAM в LA/NY/London/Frankfurt/Tokyo/Sydney/Bangalore.
  Доступность по регионам вычисляется из `subregion.unavailableSizes`.
- Денежные единицы API — mUSD (тысячные доллара): `costPerHr: 164` = $0.164/час,
  `balance: 20000` = $20.
- GPU-образа с предустановленными драйверами нет — только чистые ОС (Ubuntu 24.04 =
  version id 2284). `userScript` (cloud-init) поддерживается → драйверы ставим им.
- Для Vultr: rebuild выключен, resize включён.
- Создание сервера: `POST /servers` c `{server: {name, hostID, hostImageID, sizeID,
  regionID, sshKeys[], password, initscript}}`.

## Architecture

Python 3.11+, FastMCP, httpx (REST), asyncssh (SSH). Stateless SSH: каждый вызов
тула открывает соединение и закрывает его; долгие задачи живут в tmux на удалённой
машине (вариант A — выбран против персистентного пула сессий ради устойчивости
к рестартам MCP-сервера).

```
bitlaunch-mcp/
├── pyproject.toml              # deps: fastmcp, httpx, asyncssh
└── src/bitlaunch_mcp/
    ├── server.py               # FastMCP-приложение, определения тулов
    ├── client.py               # async REST-клиент BitLaunch
    ├── ssh.py                  # SSH-слой: exec, файлы, tmux-джобы
    └── config.py               # env-конфиг
```

**Транспорты:** stdio по умолчанию (Claude Code/Desktop запускают `uvx bitlaunch-mcp`);
`--transport http --port N` для Hermes и удалённых агентов. Один код, оба транспорта.

**Конфиг (env):**

| Переменная | Дефолт | Назначение |
|---|---|---|
| `BITLAUNCH_API_KEY` | — (обязательная) | API-токен |
| `BITLAUNCH_MAX_COST_PER_HOUR` | `1.0` | guardrail: максимум $/час за один сервер |
| `BITLAUNCH_MAX_SERVERS` | `2` | guardrail: максимум одновременных серверов |
| `BITLAUNCH_SSH_KEY_PATH` | `~/.bitlaunch-mcp/id_ed25519` | локальный SSH-ключ |

**Деньги наружу** — всегда доллары (конвертация из mUSD в client.py).

**SSH-ключ:** при первом `create_server`, если ключа нет — генерируется ed25519,
публичная часть регистрируется в BitLaunch (`POST /ssh-keys`), id ключа подставляется
во все создания. Агент о ключах не знает.

**GPU bootstrap:** для `vcg-*` планов в `initscript` автоматически инжектится
cloud-init скрипт: NVIDIA-драйверы + CUDA toolkit + tmux, git, uv. Готовность
GPU-сервера = SSH доступен и `nvidia-smi` отвечает.

## Tools (15)

### Провижeнинг

| Тул | Описание |
|---|---|
| `get_account()` | баланс $, burn rate $/час, серверов занято / лимит аккаунта |
| `list_gpu_plans()` | GPU-планы с живой доступностью: id, VRAM, CPU, RAM, $/час, список регионов где есть |
| `list_plans(plan_type?)` | все планы (standard / cpu / gpu) |
| `create_server(name, size_id, region_id, image?, wait?)` | guardrails → создание → опц. ожидание готовности (для GPU — включая nvidia-smi) |
| `get_server(server_id)` | статус, IP, uptime, накопленная стоимость $ |
| `list_servers()` | все серверы со стоимостью |
| `destroy_server(server_id)` | удалить сервер |
| `restart_server(server_id)` | перезагрузить |

### Выполнение (SSH)

| Тул | Описание |
|---|---|
| `run_command(server_id, command, timeout_s=120)` | exec → stdout, stderr, exit_code |
| `upload_file(server_id, remote_path, local_path \| content)` | файл с диска или текст напрямую |
| `download_file(server_id, remote_path, local_path)` | забрать результаты |
| `start_job(server_id, name, command, workdir?)` | tmux-сессия, лог в `~/jobs/<name>.log` |
| `get_job(server_id, name, tail=100)` | running/exited, exit code, хвост лога |
| `stop_job(server_id, name)` | убить tmux-сессию |
| `list_jobs(server_id)` | все джобы с статусами |

### Guardrails (в create_server)

1. Цена плана > `MAX_COST_PER_HOUR` → отказ с текстом лимита и именем env-переменной.
2. Активных серверов ≥ `MAX_SERVERS` → отказ.
3. Баланса меньше чем на 24ч работы плана → отказ с предложением пополнить.

## Error handling

- HTTP-ошибки BitLaunch → читаемый текст: статус + тело ответа (401 → «проверь
  BITLAUNCH_API_KEY»).
- SSH недоступен → сообщение со статусом сервера из API («ещё создаётся» и т.п.).
- `run_command` таймаут → частичный вывод + флаг `timed_out`.
- `create_server(wait=true)` → поллинг с экспоненциальным backoff, максимум 10 мин;
  по истечении — сервер возвращается как created-but-not-ready, не удаляется.
- Имена tmux-джобов валидируются (`[a-zA-Z0-9_-]+`) — они интерполируются в shell.

## Testing

1. **Юнит:** client.py + guardrails через respx (мок httpx). Фикстура — реальный
   сохранённый ответ `/hosts-create-options/1`.
2. **SSH-слой:** мок asyncssh; проверка формирования tmux-команд и парсинга статусов.
3. **MCP-интеграция:** in-memory клиент FastMCP, вызовы тулов с мокнутым бэкендом.
4. **Live smoke (opt-in, `BITLAUNCH_LIVE_TEST=1`):** создать самый дешёвый CPU-сервер,
   `run_command("echo ok")`, удалить. Стоимость — центы.

## Out of scope

- Другие хосты BitLaunch (DigitalOcean/Linode/BitLaunch) — API идентичен, hostID
  параметризуем, но тестируем и документируем только Vultr.
- DDoS protection, домены/DNS, транзакции (пополнение — вручную через сайт).
- Resize/rebuild серверов (rebuild для Vultr всё равно выключен).
- Персистентные SSH-сессии и интерактивные шеллы.
