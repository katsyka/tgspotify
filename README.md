# tgspotify

Shows the song you're listening to as the music on your Telegram profile, and keeps it updated as the track changes.

## How it works

The service polls for the currently playing track:

1. **Spotify**, if something is playing there
2. Otherwise **[YukiMusic](https://music.yukichat.lol)**, through a personal "now playing" key

When the track changes, it finds and downloads the audio with yt-dlp, adds tags and cover art, and sets the file as your profile music. This runs on your own Telegram account through Telethon. A companion bot (`/start`, owner only) shows status and lets you control the service from a chat.

## Requirements

- Python 3.10+
- ffmpeg
- A Telegram API ID and hash from [my.telegram.org](https://my.telegram.org)
- A bot token from [@BotFather](https://t.me/BotFather)
- A Spotify developer app (optional). Spotify's Web API requires the app owner to have an active Premium subscription.

## Setup

```sh
python3 -m venv venv
venv/bin/pip install telethon spotipy yt-dlp mutagen requests
```

Create `.env`:

```sh
BOT_TOKEN=...
OWNER_ID=...                # your Telegram user ID
API_ID=...
API_HASH=...
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
SPOTIFY_REDIRECT_URI=...
POLL_INTERVAL=15            # seconds, minimum 5
```

Log in to your Telegram account once, in a terminal:

```sh
set -a; . ./.env; set +a
venv/bin/python login.py
```

This creates `data/user.session`. Keep it private, because it gives full access to your account.

## Running

```sh
set -a; . ./.env; set +a
venv/bin/python bot.py
```

To run it as a service, use a systemd unit with `EnvironmentFile=` pointing to `.env`.

## YukiMusic source

In YukiMusic, open **Settings → Telegram profile music** to get a personal key, then link it from the bot panel (`/start`). When Spotify isn't playing, the service uses your YukiMusic playback instead.
