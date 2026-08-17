# The Vault — HTTPS Telegram Webhook Version

This is the original Telegram bot converted from polling to an HTTPS webhook.

## Deploy on Render

1. Put these files in a GitHub repository.
2. Create a new Render Web Service from the repository.
3. Use the included `render.yaml`, or set:
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn vault_webapp:app --host 0.0.0.0 --port $PORT`
4. Add secret environment variable:
   - `TELEGRAM_BOT_TOKEN` = your BotFather token
5. Set `PUBLIC_URL` to the HTTPS URL Render gives you, e.g.
   `https://the-vault-telegram-bot.onrender.com`
6. Deploy.

The app will automatically register:
`https://YOUR-DOMAIN/telegram/webhook`

Health check:
`https://YOUR-DOMAIN/health`

Root page:
`https://YOUR-DOMAIN/`

## Important

SQLite needs persistent storage in production. The supplied Render config
mounts a persistent disk at `/data` and uses `/data/vault.db`.

## Using a Telegram Web App / Mini App

This conversion makes the bot reachable over HTTPS, but it does not turn
the existing chat interface into a browser UI. If you want a real
Telegram Mini App with buttons, charts, a deposit form, etc., that requires
a separate HTML/JS frontend which talks to a web API.
