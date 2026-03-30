import os
import json
import requests
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta, timezone
import email.utils
import re
import socket
import time

# ------------- Конфигурация -------------
API_TOKEN = os.environ["PACHCA_API_TOKEN"]
IMAP_SERVER = os.environ.get("IMAP_SERVER", "imap.yandex.ru")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
IMAP_EMAIL = os.environ["YANDEX_MAIL_LOGIN"]
IMAP_TOKEN = os.environ["YANDEX_MAIL_TOKEN"]

# Максимум UID'ов, которые возьмём для обработки (последние N писем)
EMAIL_FETCH_LIMIT = int(os.environ.get("EMAIL_FETCH_LIMIT", "50"))
# Сколько минут назад проверяем
SINCE_MINUTES = int(os.environ.get("SINCE_MINUTES", "5"))

# network timeouts (seconds)
SOCKET_TIMEOUT = float(os.environ.get("SOCKET_TIMEOUT", "10"))
REQUESTS_TIMEOUT = float(os.environ.get("REQUESTS_TIMEOUT", "15"))

# admin fallback user id в Пачке
ADMIN_PACHCA_ID = os.environ.get("ADMIN_PACHCA_ID", "")

# Если true — помечать обработанные письма флагом \Seen (по умолчанию false)
MARK_PROCESSED_AS_SEEN = os.environ.get("MARK_PROCESSED_AS_SEEN", "false").lower() in ("1", "true", "yes")

# Увеличим _MAXLINE на случай больших заголовков
try:
    imaplib._MAXLINE = 10000000
except Exception:
    pass

# Установим глобальный сокет timeout для imaplib
socket.setdefaulttimeout(SOCKET_TIMEOUT)


# ------------- Работа с API Пачки (с кешем) -------------
_user_cache = {}

def get_user_id_by_email(email_addr):
    if not email_addr:
        return None
    email_addr = email_addr.strip().lower()
    if email_addr in _user_cache:
        print(f"🔁 Кеш найден для {email_addr}: {_user_cache[email_addr]}")
        return _user_cache[email_addr]

    print(f"🔍 Ищу пользователя по email: {email_addr}")
    url = f"https://api.pachca.com/api/shared/v1/users/?query={email_addr}"
    headers = {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"}
    try:
        response = requests.get(url, headers=headers, timeout=REQUESTS_TIMEOUT)
        print(f"➡️ GET {url} -> {response.status_code}")
        response.raise_for_status()
    except requests.exceptions.Timeout:
        print("⏱ Timeout при запросе к Pachca API (get_user_id_by_email)")
        return None
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Ошибка при запросе к Pachca API: {e}")
        return None

    try:
        data = response.json()
    except Exception as e:
        print(f"⚠️ Не удалось распарсить JSON из Pachca: {e}")
        return None

    if data.get("data"):
        user_id = data["data"][0]["id"]
        _user_cache[email_addr] = user_id
        print(f"✅ Найден пользователь {email_addr}, ID={user_id}")
        return user_id

    print(f"⚠️ Пользователь {email_addr} не найден")
    _user_cache[email_addr] = None
    return None

def send_message_to_user(entity_id, content):
    print(f"📤 Отправка сообщения пользователю {entity_id}...")
    url = "https://api.pachca.com/api/shared/v1/messages"
    headers = {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"}
    payload = {"message": {"entity_type": "user", "entity_id": entity_id, "content": content}}
    print(f"➡️ POST {url}")
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=REQUESTS_TIMEOUT)
        print(f"⬅️ Ответ Пачки: {response.status_code} {response.text}")
        response.raise_for_status()
        return response.json()
    except requests.exceptions.Timeout:
        print("⏱ Timeout при отправке сообщения в Pachca API")
        return {"error": "timeout"}
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Ошибка при отправке сообщения в Pachca API: {e}")
        return {"error": str(e)}


# ------------- IMAP login (XOAUTH2) -------------
def imap_login_oauth2(email_addr, oauth_token):
    print(f"🔌 Подключаемся к {IMAP_SERVER}:{IMAP_PORT}")
    imap = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)

    try:
        welcome = getattr(imap, "welcome", b"")
        if isinstance(welcome, bytes):
            welcome = welcome.decode("utf-8", "ignore")
        print(f"🛰  Баннер сервера: {welcome}")
    except Exception:
        pass

    try:
        typ, caps = imap.capability()
        print(f"📜 CAPABILITY: {typ} {caps}")
    except Exception as e:
        print(f"⚠️ Не удалось получить CAPABILITY: {e!r}")

    auth_raw = f"user={email_addr}\x01auth=Bearer {oauth_token}\x01\x01".encode("utf-8")
    def auth_cb(_challenge):
        return auth_raw

    print("🗝  AUTHENTICATE XOAUTH2...")
    typ, data = imap.authenticate("XOAUTH2", auth_cb)
    print(f"🔑 Результат AUTH: {typ} {data}")

    if typ != "OK":
        try:
            imap.logout()
        except Exception:
            pass
        raise imaplib.IMAP4.error(f"IMAP AUTH failed: {typ}, data={data}")

    print(f"✅ Успешный логин как {email_addr}")
    return imap


# ------------- Вспомогательные парсеры -------------
def parse_subject(header_value):
    if not header_value:
        return "(без темы)"
    try:
        parts = decode_header(header_value)
        # Собираем все части
        pieces = []
        for part, enc in parts:
            if isinstance(part, bytes):
                pieces.append(part.decode(enc or "utf-8", errors="ignore"))
            else:
                pieces.append(part)
        return "".join(pieces)
    except Exception:
        return header_value

def parse_date(header_value):
    if not header_value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(header_value)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def extract_forward_email(text: str) -> str | None:
    if not text:
        return None
    match = re.search(r"([\w\.-]+@[\w\.-]+)", text)
    if match:
        return match.group(1).lower()
    return None

def extract_issue_key(subject: str) -> str | None:
    if not subject:
        return None
    match = re.search(r"([A-Z0-9]+-\d+)", subject)
    if match:
        return match.group(1)
    return None

def clean_devnull_body(body: str) -> str:
    if not body:
        return ""
    link_match = re.search(r'<a\s+href=["\']?([^"\'>\s]+)["\']?[^>]*>подтвердите<\/a>', body, re.IGNORECASE)
    if link_match:
        url = link_match.group(1)
        body = re.sub(
            r'<a\s+href=["\']?[^"\'>\s]+["\']?[^>]*>подтвердите<\/a>',
            f"[подтвердите]({url})",
            body,
            flags=re.IGNORECASE,
        )
    body = re.sub(r"<[^>]+>", "", body)
    body = re.sub(r"(на Ваш адрес\.)(\s*Если)", r"\1\n\2", body)
    body = re.sub(r"(\n|\r|\s)*\.$", "", body.strip())
    return body.strip()


# ------------- Вспомогательная утилита: округление вниз до минуты (--- CHANGES ---) -------------
def round_down_to_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


# ------------- Основная оптимизированная функция чтения (с округлением) -------------
def fetch_recent_emails(minutes=SINCE_MINUTES, limit=EMAIL_FETCH_LIMIT):
    # --- CHANGES: округляем текущее время вниз до минуты ---
    now_utc = datetime.now(timezone.utc)
    now_rounded = round_down_to_minute(now_utc)
    cutoff_time = now_rounded - timedelta(minutes=minutes)
    print(f"⏳ Получаем письма новее {cutoff_time.isoformat()} (now={now_utc.isoformat()} rounded={now_rounded.isoformat()}), limit={limit}")
    messages = []

    mail = None
    processed_uids = set()  # дедуп в рамках одного запуска
    try:
        mail = imap_login_oauth2(IMAP_EMAIL, IMAP_TOKEN)
        mail.select("INBOX", readonly=False)

        # Формируем SINCE дату для server-side поиска (IMAP формат: 23-Oct-2025)
        # для SEARCH используем now_rounded чтобы согласовать поведение
        since_date = (now_rounded - timedelta(minutes=minutes)).strftime("%d-%b-%Y")
        search_criteria = f'(SINCE {since_date})'
        print(f"🔎 IMAP SEARCH {search_criteria}")
        typ, data = mail.uid("search", None, search_criteria)
        if typ != "OK":
            print(f"⚠️ SEARCH returned {typ}")
            return []

        all_uids = data[0].split() if data and data[0] else []
        total = len(all_uids)
        print(f"📨 Найдено UID'ов (SINCE): {total}")

        if total == 0:
            return []

        # Берём последние `limit` UID (с конца)
        selected = all_uids[-limit:]
        print(f"📌 Обработаем {len(selected)} последних UID'ов (limit={limit})")

        # Проходим с конца (от новых к старым)
        for uid in reversed(selected):
            uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)

            # защита от дублей в рамках одного запуска
            if uid_str in processed_uids:
                print(f"🔁 UID {uid_str} уже обработан в этом запуске — пропускаем")
                continue

            print(f"➡️ Header fetch UID {uid_str}")
            typ, hdr_data = mail.uid('fetch', uid_str, '(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT)])')
            if typ != "OK" or not hdr_data:
                print(f"⚠️ Не удалось получить заголовки для UID {uid_str} ({typ})")
                continue

            hdr_bytes = None
            for part in hdr_data:
                if isinstance(part, tuple) and isinstance(part[1], (bytes, bytearray)):
                    hdr_bytes = part[1]
                    break
            if not hdr_bytes:
                print(f"⚠️ Заголовки пустые для UID {uid_str}")
                continue

            try:
                hdr_msg = email.message_from_bytes(hdr_bytes)
            except Exception as e:
                print(f"⚠️ Ошибка парсинга заголовков UID {uid_str}: {e}")
                continue

            raw_date = hdr_msg.get("Date")
            msg_date = parse_date(raw_date)
            if not msg_date:
                print(f"⚠️ Не удалось распарсить дату для UID {uid_str}: {raw_date!r}")
                continue

            # Если письмо старше cutoff — пропускаем
            if msg_date < cutoff_time:
                print(f"⏭ UID {uid_str} пропускаем — дата {msg_date.isoformat()} < cutoff {cutoff_time.isoformat()}")
                continue

            raw_from = hdr_msg.get("From") or ""
            raw_to = hdr_msg.get("To") or ""
            from_addr = email.utils.parseaddr(raw_from)[1].lower() if raw_from else ""
            to_addr = email.utils.parseaddr(raw_to)[1].lower() if raw_to else ""

            subject = parse_subject(hdr_msg.get("Subject"))

            body_text = ""
            # Если это devnull — нужно тело, тогда уже запросим full BODY (медленнее)
            if from_addr == "devnull@yandex.ru":
                print(f"🔎 Получаем тело для devnull UID {uid_str}")
                typ, full_data = mail.uid('fetch', uid_str, '(BODY.PEEK[])')
                if typ != "OK" or not full_data:
                    print(f"⚠️ Не удалось получить тело для UID {uid_str}")
                else:
                    for part in full_data:
                        if isinstance(part, tuple) and isinstance(part[1], (bytes, bytearray)):
                            try:
                                full_msg = email.message_from_bytes(part[1])
                            except Exception:
                                full_msg = None
                            if full_msg:
                                if full_msg.is_multipart():
                                    for p in full_msg.walk():
                                        if p.get_content_type() == "text/plain":
                                            try:
                                                body_text = p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", errors="ignore")
                                            except Exception:
                                                body_text = ""
                                            break
                                    if not body_text:
                                        for p in full_msg.walk():
                                            if p.get_content_type() == "text/html":
                                                try:
                                                    body_text = p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", errors="ignore")
                                                except Exception:
                                                    body_text = ""
                                                break
                                else:
                                    try:
                                        body_text = full_msg.get_payload(decode=True).decode(full_msg.get_content_charset() or "utf-8", errors="ignore")
                                    except Exception:
                                        body_text = ""
                                break

                # отмечаем как прочитанное (для devnull всегда так было)
                try:
                    mail.uid('store', uid_str, '+FLAGS', '(\\Seen)')
                except Exception:
                    pass

            # Добавим в processed set
            processed_uids.add(uid_str)

            # Если включена опция — пометим как Seen (чтобы уменьшить вероятность дублирования между запусками)
            if MARK_PROCESSED_AS_SEEN and from_addr != "devnull@yandex.ru":
                try:
                    mail.uid('store', uid_str, '+FLAGS', '(\\Seen)')
                except Exception:
                    pass

            messages.append({
                "uid": uid_str,
                "date": msg_date.isoformat(),
                "from": from_addr,
                "to": to_addr,
                "subject": subject,
                "body": body_text
            })

        return messages

    except Exception as e:
        print(f"❌ Ошибка в fetch_recent_emails: {e}")
        return []
    finally:
        try:
            if mail is not None:
                try:
                    mail.close()
                except Exception:
                    pass
                try:
                    mail.logout()
                except Exception:
                    pass
        except Exception:
            pass


# ------------- Остальной код остался логически прежним -------------
def handler(event, context):
    print("🚀 Старт handler")
    results = []

    try:
        recent_emails = fetch_recent_emails(minutes=SINCE_MINUTES, limit=EMAIL_FETCH_LIMIT)
    except Exception as e:
        print(f"❌ Ошибка при fetch_recent_emails: {e}")
        return {"statusCode": 502, "body": json.dumps({"error": str(e)})}

    print(f"📥 Обработаем {len(recent_emails)} писем")
    for mail_item in recent_emails:
        sender = mail_item.get("from", "")
        recipient = mail_item.get("to", "")
        subject = mail_item.get("subject", "(нет темы)")
        body = mail_item.get("body", "")

        # --- Особый кейс: devnull@yandex.ru ---
        if sender.lower() == "devnull@yandex.ru":
            print(f"⚙️ Обработка письма от devnull для {recipient}")
            forward_email = extract_forward_email(body)
            if forward_email:
                user_id = get_user_id_by_email(forward_email)
                cleaned = clean_devnull_body(body)
                if user_id:
                    resp = send_message_to_user(user_id, f"📩 {cleaned}")
                    results.append({"email": forward_email, "status": "success", "response": resp})
                else:
                    fallback = f"⚠️ Email {forward_email} не найден в Пачке.\nПисьмо:\n{cleaned}"
                    send_message_to_user(ADMIN_PACHCA_ID, fallback)
                    results.append({"email": forward_email, "status": "not_found"})
            else:
                print("⚠️ devnull письмо, но не найден email в тексте")
            continue

        # --- Обычные уведомления ---
        issue_key = extract_issue_key(subject)
        if issue_key:
            tracker_url = f"https://tracker.yandex.ru/{issue_key}"
            content = f"📩 Новое уведомление из Трекера:\n{subject}\n🔗 {tracker_url}"
        else:
            content = f"📩 Новое уведомление из Трекера:\n{subject}"

        print(f"➡️ Обработка письма для {recipient}")

        if recipient.lower() == "send.pachca@bnovo.ru":
            print("⏭ Пропускаем системный адрес send.pachca@bnovo.ru")
            continue
        else:
            user_id = get_user_id_by_email(recipient)
            if user_id:
                resp = send_message_to_user(user_id, content)
                results.append({"email": recipient, "status": "success", "response": resp})
            else:
                fallback = f"⚠️ Email {recipient} не найден в Пачке.\nПисьмо:\n{content}"
                send_message_to_user(ADMIN_PACHCA_ID, fallback)
                results.append({"email": recipient, "status": "not_found"})

    return {"statusCode": 200, "body": json.dumps(results)}