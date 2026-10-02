"""Один раз запустить ЛОКАЛЬНО: откроет браузер, ты входишь в рабочий Google-аккаунт
и разрешаешь отправку писем и управление календарём. Скрипт сам допишет
GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN в .env (на экран их не выводит).

    pip install google-auth-oauthlib
    python google_auth.py google_client.json
"""
import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    # только чтобы прочитать подпись из настроек Gmail (более узкого scope у Google нет)
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/calendar.events",
    # встречи с коллегами: каталог сотрудников и «свободен/занят» без деталей
    "https://www.googleapis.com/auth/directory.readonly",
    "https://www.googleapis.com/auth/calendar.freebusy",
]
ENV = Path(__file__).resolve().parent / ".env"


def set_env(values: dict) -> None:
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    lines = [ln for ln in lines if ln.split("=", 1)[0].strip() not in values]
    lines += [f"{k}={v}" for k, v in values.items()]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    client_file = Path(sys.argv[1] if len(sys.argv) > 1 else "google_client.json")
    flow = InstalledAppFlow.from_client_secrets_file(client_file, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    if not creds.refresh_token:
        sys.exit("Google не выдал refresh token — отзови доступ приложения в аккаунте и запусти ещё раз.")
    cfg = json.loads(client_file.read_text(encoding="utf-8"))
    cfg = cfg.get("installed") or cfg.get("web")
    set_env({
        "GOOGLE_CLIENT_ID": cfg["client_id"],
        "GOOGLE_CLIENT_SECRET": cfg["client_secret"],
        "GOOGLE_REFRESH_TOKEN": creds.refresh_token,
    })
    print("Готово: доступ сохранён в .env")


if __name__ == "__main__":
    main()
