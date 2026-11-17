# One-off terminal login: creates data/user.session for bot.py
import os
from telethon.sync import TelegramClient
c = TelegramClient("data/user", int(os.environ["API_ID"]), os.environ["API_HASH"],
                   device_model="tgspotify", system_version="Linux")
c.start()  # prompts for phone, code, 2FA password
print("Logged in as", c.get_me().first_name)
c.disconnect()
