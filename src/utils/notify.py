import os
import requests
from dotenv import load_dotenv

load_dotenv()

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"


def notify(title: str, message: str, priority: int = 0) -> bool:
    """
    Send a Pushover push notification to your phone.
    Returns True if sent successfully, False if keys are missing or request fails.
    Fails silently so the pipeline never breaks if Pushover is unavailable.

    Priority levels:
        -2  no notification, no sound
        -1  quiet notification
         0  normal (default)
         1  high priority, bypasses quiet hours
    """
    token = os.getenv("PUSHOVER_APP_TOKEN", "")
    user  = os.getenv("PUSHOVER_USER_KEY", "")

    if not token or not user:
        return False

    try:
        resp = requests.post(
            PUSHOVER_API_URL,
            data={
                "token":    token,
                "user":     user,
                "title":    title,
                "message":  message,
                "priority": priority,
            },
            timeout=5,
        )
        return resp.status_code == 200
    except Exception:
        return False


def notify_and_wait(title: str, message: str, prompt: str = "Press ENTER to continue: "):
    """
    Send a Pushover notification then block on keyboard Enter.
    Use this everywhere the pipeline needs human confirmation.
    """
    sent = notify(title, message, priority=1)
    if sent:
        print(f"📲 Notification sent → {title}")
    print(f"⏳ {message}")
    input(prompt)
