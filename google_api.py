"""Gmail (отправка письма) и Google Calendar (событие «Не на місці») от имени пользователя.

Доступ — OAuth refresh token, полученный один раз скриптом google_auth.py.
"""
import base64
import html
import json
import logging
import re
import time
import uuid
from datetime import datetime
from email.message import EmailMessage

import httpx

from config import (
    BASE_DIR, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN, TZ, WORK_EMAIL_FROM, WORK_EMAIL_TO,
)

log = logging.getLogger("reminder_bot.google")
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    # только чтобы прочитать подпись из настроек Gmail (более узкого scope у Google нет)
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/calendar.events",
    # встречи с коллегами: каталог сотрудников и «свободен/занят» без деталей
    "https://www.googleapis.com/auth/directory.readonly",
    "https://www.googleapis.com/auth/calendar.freebusy",
]
OOO_DECLINE = "Не на місці. Відповім, щойно повернуся."

_token = {"value": None, "exp": 0.0}


class GoogleError(Exception):
    pass


def configured() -> bool:
    return all((GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN, WORK_EMAIL_FROM, WORK_EMAIL_TO))


async def _access_token(client: httpx.AsyncClient) -> str:
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    r = await client.post("https://oauth2.googleapis.com/token", data={
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": GOOGLE_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    })
    if r.status_code != 200:
        log.error("Google token %s: %s", r.status_code, r.text[:300])
        raise GoogleError("нет доступа к Google — возможно, нужно заново выполнить google_auth.py")
    data = r.json()
    _token.update(value=data["access_token"], exp=time.time() + data.get("expires_in", 3600))
    return _token["value"]


async def _signature(client: httpx.AsyncClient, token: str) -> str:
    """HTML-подпись адреса отправителя из настроек Gmail ('' если нет или не удалось)."""
    r = await client.get(
        f"https://gmail.googleapis.com/gmail/v1/users/me/settings/sendAs/{WORK_EMAIL_FROM}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if r.status_code != 200:
        # письмо важнее подписи: отправим без неё
        log.warning("Подпись Gmail не прочиталась %s: %s", r.status_code, r.text[:300])
        return ""
    return r.json().get("signature", "") or ""


def _html_to_text(html_: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li)>", "\n", html_)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def send_email(subject: str, body: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _access_token(client)
        signature = await _signature(client, token)
        msg = EmailMessage()
        msg["From"] = WORK_EMAIL_FROM
        msg["To"] = WORK_EMAIL_TO
        msg["Subject"] = subject
        if signature:
            msg.set_content(f"{body}\n\n{_html_to_text(signature)}")
            body_html = html.escape(body).replace("\n", "<br>")
            msg.add_alternative(
                f'<div dir="ltr">{body_html}<br><br>'
                f'<div class="gmail_signature">{signature}</div></div>',
                subtype="html",
            )
        else:
            msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        r = await client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {token}"}, json={"raw": raw},
        )
    if r.status_code != 200:
        log.error("Gmail send %s: %s", r.status_code, r.text[:500])
        raise GoogleError(f"Gmail ответил {r.status_code}")


async def create_ooo(start, end) -> str:
    """Событие типа «Не на місці» (outOfOffice) в основном календаре. Возвращает ссылку."""
    event = {
        "eventType": "outOfOffice",
        "summary": "Не на місці",
        "start": {"dateTime": start.isoformat(), "timeZone": str(TZ)},
        "end": {"dateTime": end.isoformat(), "timeZone": str(TZ)},
        "transparency": "opaque",
        "outOfOfficeProperties": {
            "autoDeclineMode": "declineOnlyNewConflictingInvitations",
            "declineMessage": OOO_DECLINE,
        },
    }
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _access_token(client)
        r = await client.post(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {token}"}, json=event,
        )
    if r.status_code != 200:
        log.error("Calendar insert %s: %s", r.status_code, r.text[:500])
        raise GoogleError(f"Календарь ответил {r.status_code}")
    return r.json().get("htmlLink", "")


# ---------- встречи с коллегами ----------

DIRECTORY_CACHE = BASE_DIR / "directory.json"
DIRECTORY_TTL = 24 * 3600


async def directory(force: bool = False) -> list[dict]:
    """Сотрудники своего домена [{"name", "email"}] из Google Workspace (кеш на сутки в directory.json)."""
    if not force and DIRECTORY_CACHE.exists() and time.time() - DIRECTORY_CACHE.stat().st_mtime < DIRECTORY_TTL:
        return json.loads(DIRECTORY_CACHE.read_text(encoding="utf-8"))
    people, page = [], None
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _access_token(client)
        while True:
            params = {
                "readMask": "names,emailAddresses",
                "sources": "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE",
                "pageSize": 1000,
            }
            if page:
                params["pageToken"] = page
            r = await client.get(
                "https://people.googleapis.com/v1/people:listDirectoryPeople",
                headers={"Authorization": f"Bearer {token}"}, params=params,
            )
            if r.status_code != 200:
                log.error("Directory %s: %s", r.status_code, r.text[:500])
                raise GoogleError(f"каталог сотрудников недоступен ({r.status_code})")
            data = r.json()
            for p in data.get("people", []):
                emails = [e["value"] for e in p.get("emailAddresses", []) if e.get("value")]
                name = (p.get("names") or [{}])[0].get("displayName") or ""
                # каталог общий на всю группу компаний (2000+ записей) — берём только свой домен
                if (emails and emails[0].lower() != WORK_EMAIL_FROM.lower()
                        and emails[0].lower().endswith("@" + WORK_EMAIL_FROM.split("@")[1].lower())):
                    people.append({"name": name or emails[0], "email": emails[0]})
            page = data.get("nextPageToken")
            if not page:
                break
    people.sort(key=lambda p: p["name"])
    DIRECTORY_CACHE.write_text(json.dumps(people, ensure_ascii=False), encoding="utf-8")
    return people


async def freebusy(emails: list[str], start, end) -> tuple[dict, list[str]]:
    """{email: [(start, end), ...]} занятых интервалов и список адресов, чью занятость не видно."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _access_token(client)
        r = await client.post(
            "https://www.googleapis.com/calendar/v3/freeBusy",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "timeMin": start.isoformat(), "timeMax": end.isoformat(), "timeZone": str(TZ),
                "items": [{"id": e} for e in emails],
            },
        )
    if r.status_code != 200:
        log.error("freeBusy %s: %s", r.status_code, r.text[:500])
        raise GoogleError(f"не получилось проверить занятость ({r.status_code})")
    busy, hidden = {}, []
    for email, cal in r.json().get("calendars", {}).items():
        if cal.get("errors"):
            hidden.append(email)
        busy[email] = [
            (datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(TZ),
             datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(TZ))
            for b in cal.get("busy", [])
        ]
    return busy, hidden


async def create_meeting(title: str, start, end, emails: list[str], notes: str | None, meet: bool) -> dict:
    """Встреча в основном календаре с приглашениями участникам. {"link", "meet"}."""
    event = {
        "summary": title,
        "start": {"dateTime": start.isoformat(), "timeZone": str(TZ)},
        "end": {"dateTime": end.isoformat(), "timeZone": str(TZ)},
        "attendees": [{"email": e} for e in emails],
    }
    if notes:
        event["description"] = notes
    if meet:
        event["conferenceData"] = {"createRequest": {
            "requestId": uuid.uuid4().hex, "conferenceSolutionKey": {"type": "hangoutsMeet"},
        }}
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _access_token(client)
        r = await client.post(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {token}"},
            params={"conferenceDataVersion": 1, "sendUpdates": "all"},
            json=event,
        )
    if r.status_code != 200:
        log.error("Meeting insert %s: %s", r.status_code, r.text[:500])
        raise GoogleError(f"Календарь ответил {r.status_code}")
    data = r.json()
    return {"link": data.get("htmlLink", ""), "meet": data.get("hangoutLink", "")}


async def list_events(time_min, time_max) -> list[dict]:
    """Мои встречи (я организатор, есть участники) в интервале, по времени начала."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _access_token(client)
        r = await client.get(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {token}"},
            params={"timeMin": time_min.isoformat(), "timeMax": time_max.isoformat(),
                    "singleEvents": "true", "orderBy": "startTime", "maxResults": 250},
        )
    if r.status_code != 200:
        log.error("Events list %s: %s", r.status_code, r.text[:500])
        raise GoogleError(f"не получилось прочитать календарь ({r.status_code})")
    out = []
    for e in r.json().get("items", []):
        if e.get("status") == "cancelled" or "dateTime" not in e.get("start", {}):
            continue  # отменённые и события на весь день
        if e.get("eventType", "default") != "default" or not e.get("organizer", {}).get("self"):
            continue
        out.append({
            "id": e["id"],
            "title": e.get("summary") or "(без названия)",
            "start": datetime.fromisoformat(e["start"]["dateTime"]).astimezone(TZ).isoformat(),
            "end": datetime.fromisoformat(e["end"]["dateTime"]).astimezone(TZ).isoformat(),
            "attendees": [a["email"] for a in e.get("attendees", []) if not a.get("self")],
        })
    return out


async def delete_event(event_id: str) -> None:
    """Отменяет встречу; участники получают уведомление. Уже удалённая — не ошибка."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _access_token(client)
        r = await client.delete(
            f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{event_id}",
            headers={"Authorization": f"Bearer {token}"}, params={"sendUpdates": "all"},
        )
    if r.status_code not in (200, 204, 404, 410):
        log.error("Event delete %s: %s", r.status_code, r.text[:500])
        raise GoogleError(f"Календарь ответил {r.status_code}")


async def move_event(event_id: str, start, end) -> dict:
    """Переносит встречу; участники получают обновление. {"link", "meet"}."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _access_token(client)
        r = await client.patch(
            f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{event_id}",
            headers={"Authorization": f"Bearer {token}"}, params={"sendUpdates": "all"},
            json={"start": {"dateTime": start.isoformat(), "timeZone": str(TZ)},
                  "end": {"dateTime": end.isoformat(), "timeZone": str(TZ)}},
        )
    if r.status_code != 200:
        log.error("Event move %s: %s", r.status_code, r.text[:500])
        raise GoogleError(f"Календарь ответил {r.status_code}")
    data = r.json()
    return {"link": data.get("htmlLink", ""), "meet": data.get("hangoutLink", "")}


async def agenda(time_min, time_max) -> list[dict]:
    """Все мои события в интервале (кроме отменённых и тех, от которых я отказался)."""
    items, page = [], None
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _access_token(client)
        while True:
            params = {"timeMin": time_min.isoformat(), "timeMax": time_max.isoformat(),
                      "singleEvents": "true", "orderBy": "startTime", "maxResults": 250}
            if page:
                params["pageToken"] = page
            r = await client.get(
                "https://www.googleapis.com/calendar/v3/calendars/primary/events",
                headers={"Authorization": f"Bearer {token}"}, params=params,
            )
            if r.status_code != 200:
                log.error("Agenda %s: %s", r.status_code, r.text[:500])
                raise GoogleError(f"не получилось прочитать календарь ({r.status_code})")
            data = r.json()
            items += data.get("items", [])
            page = data.get("nextPageToken")
            if not page:
                break
    out = []
    for e in items:
        me = next((a for a in e.get("attendees", []) if a.get("self")), {})
        if e.get("status") == "cancelled" or me.get("responseStatus") == "declined":
            continue
        all_day = "date" in e.get("start", {})
        if all_day:
            start, end = e["start"]["date"], e["end"]["date"]
        else:
            start = datetime.fromisoformat(e["start"]["dateTime"]).astimezone(TZ).isoformat()
            end = datetime.fromisoformat(e["end"]["dateTime"]).astimezone(TZ).isoformat()
        out.append({
            "id": e["id"],
            "title": e.get("summary") or "(без названия)",
            "start": start, "end": end, "all_day": all_day,
            "type": e.get("eventType", "default"),
            "meet": e.get("hangoutLink", ""),
            "guests": len([a for a in e.get("attendees", []) if not a.get("self") and not a.get("resource")]),
            "mine": bool(e.get("organizer", {}).get("self")),
            "response": me.get("responseStatus", "accepted"),
            "attendees": [a["email"] for a in e.get("attendees", []) if not a.get("self")],
        })
    return out
