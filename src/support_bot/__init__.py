import os

# python-telegram-bot warns on RetryAfter.retry_after unless we opt in to the timedelta API.
os.environ.setdefault("PTB_TIMEDELTA", "true")
