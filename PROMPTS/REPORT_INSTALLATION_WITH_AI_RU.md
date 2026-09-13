# Передать улучшения после установки через AI

Этот prompt предназначен для Claude Code или Codex, который уже разворачивал проект,
нашёл ошибки и мог оставить локальные незапушенные commits.

```text
Проанализируй полностью нашу прошлую сессию установки Telegram MCP и подготовь безопасный
Pull Request в upstream:
https://github.com/salto-agancy/telegram-mcp-ai-deploy

Сначала прочитай AGENTS.md, CONTRIBUTING.md, SECURITY.md,
PROMPTS/CONTRIBUTE_WITH_AI.md и текущий upstream main.

Твоя задача:
1. Найди в локальном clone все commits, незакоммиченные изменения и deployment-ошибки,
   появившиеся во время установки. Не полагайся только на итоговое сообщение: сопоставь
   симптомы, команды, root cause, diff и фактический результат healthcheck.
2. Сравни каждое исправление с актуальным upstream main. Не переноси уже исправленное и не
   cherry-pick весь старый commit вслепую.
3. Для каждого отсутствующего исправления создай минимальный regression test, который падает
   на upstream main и проходит после fix. Для infrastructure-изменений проверь Compose,
   Dockerfile, Linux/macOS portability, non-root permissions, отсутствие host ports и
   необходимый outbound egress.
4. Удали из отчёта, тестов, fixtures, branch history и PR любые реальные значения: Telegram
   API credentials и sessions, телефоны, имена/ID чатов и пользователей, bearer/OAuth/
   Cloudflare tokens, tunnel IDs, домены, VPS IP, SSH aliases, абсолютные локальные пути,
   логи сообщений и production ACL. Используй только синтетические примеры.
5. Если любой секрет уже оказался в локальном commit, не push этот commit. Создай новую
   чистую branch от upstream main, перенеси только безопасный код вручную, повтори full-history
   scan и сообщи владельцу, какой credential нужно отозвать.
6. Сохрани secure defaults: read-only, Saved Messages only, raw MTProto disabled, setup portal
   только через VPS loopback + SSH local-forward, никаких публичных backend/setup ports.
7. Обнови документацию и CHANGELOG.md в Unreleased. Запусти `make check`,
   `make docker-validate` для deployment-файлов и Gitleaks/full-history scanning.
8. Используй fork → focused branch → Pull Request. Если fork ещё нет — создай через GitHub CLI.
   Если GitHub CLI не авторизован, сам запусти browser/device login и попроси меня только
   подтвердить GitHub authorization. Не проси меня вручную вводить git-команды.
9. В PR опиши только sanitized: симптом, root cause, решение, security impact, тесты и
   совместимость. Не меняй production и не отправляй сообщения Telegram во время проверки.

В конце дай URL Pull Request, перечень включённых fixes, результаты тестов и только те
действия, которые технически нельзя завершить без моего подтверждения.
```
