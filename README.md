# Advanced yt-dlp Telegram Bot — Railway

A Telegram bot wrapper around yt-dlp with inline menus, HTML rich text, video/audio choices, 720p/1080p choices, progress updates, `/info`, `/settings`, `/cancel`, a configurable sticker button, and a premium information button.

## Deploy
1. Create a bot with Telegram `@BotFather` → `/newbot`.
2. Create a GitHub repository and upload the contents of this folder to its root.
3. Railway → New Project → Deploy from GitHub Repo → select the repository.
4. Railway service → Variables → add `BOT_TOKEN` with the BotFather token.
5. Deploy and check the logs. It uses long polling; no domain or webhook is required.
6. Open your bot and send `/start`.

## Optional Railway variables
- `BOT_NAME`: display name shown in the menu.
- `STICKER_FILE_ID`: Telegram sticker file ID used by the Stickers button.
- `PREMIUM_TEXT`: custom HTML text shown by the Premium button.
- `MAX_UPLOAD_MB`: maximum upload size (clamped to 49 MB).
- `MAX_CONCURRENT_DOWNLOADS`: 1–4 concurrent downloads per service.
- `LOG_LEVEL`: INFO or DEBUG.

## Bot commands
- `/start`, `/help`, `/settings`, `/about`
- `/cancel`
- `/audio URL`
- `/video URL`
- `/info URL`

## Notes
- The Premium button is an information/menu button, not a paid subscription system. Telegram Stars payments and entitlement checks must be added separately if you want actual paid premium access.
- Sticker sending requires a valid sticker `file_id` in `STICKER_FILE_ID`; the bot cannot conjure a Premium sticker from a button alone.
- Uses Telegram's standard bot upload size ceiling conservatively (49 MB). Large files may fail or be rejected; use a lower quality.
- Downloads are temporary and removed after sending. Railway's filesystem is ephemeral.
- A single running Railway replica is recommended because this bot uses long polling.
- Use only for content you are authorized to download; follow copyright law and each platform's terms. Some websites may block downloads or require supported authentication, which this template intentionally does not collect or store.
