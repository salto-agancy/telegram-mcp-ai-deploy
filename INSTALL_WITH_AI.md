# Установка с помощью AI coding-agent

Откройте клон репозитория в Claude Code, Codex или другом подходящем
coding-agent и вставьте ему целиком следующий prompt.

```text
Полностью разверни Telegram MCP из публичного репозитория:
https://github.com/salto-agancy/telegram-mcp-ai-deploy

Если текущая рабочая папка ещё не является его клоном, самостоятельно клонируй публичный
репозиторий, перейди в его корень и продолжи работу там. GitHub collaborator access или
авторизация для клонирования не нужны.

Оставь мне готовое рабочее подключение к Telegram MCP.

Действуй как ответственный за deployment. Сначала прочитай AGENTS.md, QUICKSTART.md,
docs/ARCHITECTURE_DECISION.md, docs/DEPLOYMENT.md, docs/CLOUDFLARE.md,
docs/AUTH.md и инструкцию для выбранного мной MCP-клиента.

Правила:
1. Сначала исследуй, потом спрашивай. Самостоятельно определи мою ОС, состояние Git,
   наличие Docker/Compose, SSH-конфигурацию, доступные Ubuntu VPS, существующие
   .env/.runtime.env/secrets, сведения о домене и Cloudflare, а также текущую конфигурацию
   MCP-клиента. Читай только необходимые метаданные; не раскрывай и не загружай значения
   посторонних секретов.
2. Задай один компактный набор вопросов только о том, что невозможно безопасно определить:
   целевой VPS/SSH host, hostname MCP, разрешение подготовить VPS, тип Telegram-аккаунта
   и необходимые credentials, read-only или write-доступ с разрешёнными чатами, выбранный
   MCP-клиент. По умолчанию используй read-only, Saved Messages и отключённый raw MTProto.
3. Никогда не проси вставлять секреты в Git или tracked-файлы. Не проси пользователя
   открывать терминал и вручную запускать SSH, Docker или shell-команды: запускай их сам
   через доступ coding-agent. Cloudflare credentials получай через защищённый prompt
   coding-agent либо игнорируемый файл с правами 600.
   Не выводи секреты, не помещай их в аргументы команд, видимые в списке процессов, логи,
   чат, commits или финальный отчёт.
4. Используй scoped Cloudflare API Token с правами Account:Cloudflare Tunnel:Edit,
   Zone:DNS:Edit и Zone:Zone:Read. Никогда не запрашивай Global API Key. Используй token
   только во время provisioning, а затем сообщи, что его можно отозвать.
5. Не открывай backend-порты наружу. Используй подготовленные приватную Docker Compose
   network и remotely managed Cloudflare Tunnel. Не отключай TLS, bearer authentication,
   OAuth, ACL или secret scanning ради обхода ошибки.
6. Во время установки и проверки никогда не отправляй, не редактируй и не удаляй сообщения
   Telegram. Финальный Telegram smoke test должен использовать только read-only вызов
   get_messages.

Выполнение:
- Если deployment удалённый, склонируй публичный репозиторий на VPS
  через SSH, не перенося мои локальные credential-файлы. Перед изменением существующего
  deployment сделай защищённый backup.
- Выполни scripts/preflight.sh base, затем scripts/bootstrap.sh. Если членство в группе
  Docker требует новой SSH-сессии, переподключись один раз.
- Выполни scripts/init-secrets.sh. Заполни .env и .runtime.env только обнаруженными или
  полученными значениями, установи права 600 и не показывай их содержимое.
- Для user-account сначала выполни на VPS `scripts/telegram-login-web.sh start`. Сам создай
  локальный SSH forward к выведенному loopback-порту и сам открой в моём браузере страницу
  `/setup?branch=new-session`. Я только сканирую автоматически обновляемый QR в Telegram и,
  если потребуется, ввожу 2FA-пароль в защищённую web-форму. Не проси меня открывать
  Terminal. Сам опрашивай `scripts/telegram-login-web.sh status`, после успеха выполни
  `scripts/telegram-login-web.sh finish` и закрой SSH forward. Никогда не публикуй setup-port.
- `scripts/telegram-login.sh` используй только как fallback для bot-account или если QR-login
  технически недоступен; PTY всё равно запускает агент, а не пользователь.
- Выполни scripts/cloudflare-provision.sh. Он должен через API создать или согласовать
  tunnel, ingress и DNS, а затем локально сохранить только connector token. Не отправляй
  меня выполнять действия в панели Cloudflare, если API работает.
- Выполни scripts/deploy.sh, затем TELEGRAM_SMOKE=true scripts/healthcheck.sh. Не объявляй
  deployment завершённым, пока команда не выведет DEPLOYMENT PASSED.
- Проверь restart policy: один раз перезапусти Compose services и повтори healthcheck.
  Проверь DNS, HTTPS, OAuth metadata, MCP initialize, tools/list и read-only доступ
  к Telegram.
- Настрой выбранный MCP-клиент по шаблону из templates/. Перед изменением существующей
  конфигурации сделай backup и аккуратно объедини настройки, не заменяя файл целиком.
  Если web-клиент требует ручного действия в UI или показывает callback URL, подготовь всё
  остальное и оставь мне ровно это финальное действие. При необходимости добавь callback
  в MCP_OAUTH_REDIRECT_URIS и повтори deployment.
- Выполни scripts/secret-scan.sh --worktree --history и git diff --check. Не отправляй
  runtime-конфигурацию в GitHub и не меняй настройки upstream repository.

При ошибке самостоятельно установи причину, исправь её в репозитории и продолжи с упавшего
идемпотентного этапа.

В финале сообщи только:
- DEPLOYMENT PASSED либо FAILED AT / CAUSE / NEXT AUTOMATIC ACTION;
- публичные MCP URL без tokens;
- выбранный профиль доступа и количество tools;
- результаты тестов;
- одно или два неизбежных действия на стороне MCP-клиента;
- какие временные credentials теперь можно отозвать.
```
