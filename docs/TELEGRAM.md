# Telegram credentials and permissions

The full product uses a Telegram user account through Telethon/MTProto. Obtain `api_id` and
`api_hash` from https://my.telegram.org/apps, place them in ignored `.runtime.env`, and run
`scripts/telegram-login.sh`. Enter OTP and optional 2FA only in its interactive prompt.
The resulting `.session` lives in a Docker volume and is equivalent to a logged-in device.

A BotFather token can replace phone login, but bots cannot read arbitrary personal history;
high-level tools may be restricted. Use a user account for inbox/search use cases.

Default ACL grants read-only Saved Messages (`me`). Set comma-separated numeric chat IDs or
`@usernames` in `TELEGRAM_ALLOWED_CHATS`; change `TELEGRAM_ACCESS_MODE=write` only after the
operator explicitly accepts send/edit capability. Login-code peer `777000`, BotFather, and
SpamBot are blocked regardless of the lane. Raw MTProto remains off.

Telegram may rate-limit reads (`FloodWait`). The tool layer returns the error; agents should
wait for the specified interval, narrow queries, and use pagination rather than blind retry.
