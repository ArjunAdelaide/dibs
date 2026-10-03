"""Settings, read from environment variables or a local .env file."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


_load_dotenv()

# Any OpenAI-compatible endpoint works. Default is Gemini's free tier.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-flash-lite-latest")
LLM_FALLBACK_MODELS = [m.strip() for m in os.getenv("LLM_FALLBACK_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.5-flash").split(",") if m.strip()]

# Model providers: any OpenAI-compatible endpoint. The default one comes from LLM_BASE_URL / LLM_API_KEY;
# add more with PROVIDER_<NAME>_URL and PROVIDER_<NAME>_KEY.
DEFAULT_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
PROVIDERS = {DEFAULT_PROVIDER: (LLM_BASE_URL, LLM_API_KEY)}
for _name, _value in os.environ.items():
    if _name.startswith("PROVIDER_") and _name.endswith("_URL"):
        _provider = _name[len("PROVIDER_"):-len("_URL")]
        PROVIDERS[_provider.lower()] = (_value, os.getenv(f"PROVIDER_{_provider}_KEY", ""))


def _chain(env_name: str) -> list[str]:
    """A job's models, first choice then backups. Defaults to LLM_MODEL then LLM_FALLBACK_MODELS."""
    listed = [m.strip() for m in os.getenv(env_name, "").split(",") if m.strip()]
    return listed or [LLM_MODEL] + [m for m in LLM_FALLBACK_MODELS if m != LLM_MODEL]


LLM_CHAINS = {"chat": _chain("LLM_CHAT"), "browser": _chain("LLM_BROWSER"), "memory": _chain("LLM_MEMORY")}

DB_PATH = Path(os.getenv("DIBS_DB", str(ROOT / "data" / "dibs.db")))
VENUES_PATH = ROOT / "data" / "venues.json"
DEALS_PATH = ROOT / "data" / "deals.json"
EVENTS_PATH = ROOT / "data" / "events.json"
MEMORY_DIR = Path(os.getenv("DIBS_MEMORY", str(ROOT / "data" / "memory")))

CITY = os.getenv("DIBS_CITY", "Adelaide")
TIMEZONE = os.getenv("DIBS_TZ", "Australia/Adelaide")
REGION = os.getenv("DIBS_REGION", "South Australia, Australia")  # place names are looked up here first
# The centre of the home city. Inside this circle Dibs uses the hand-checked venue list; outside, it finds venues live.
HOME_LAT = float(os.getenv("DIBS_HOME_LAT", "-34.9285"))
HOME_LON = float(os.getenv("DIBS_HOME_LON", "138.6007"))
HOME_RADIUS_KM = float(os.getenv("DIBS_HOME_RADIUS_KM", "80"))

# iMessage bridge safety: only these handles get replies, and nothing is sent while DRY_RUN=1.
ALLOWLIST = {h.strip() for h in os.getenv("ALLOWLIST", "").split(",") if h.strip()}
OPEN_ACCESS = "*" in ALLOWLIST  # ALLOWLIST=* lets anyone text the agent
BLOCKLIST = {h.strip() for h in os.getenv("BLOCKLIST", "").split(",") if h.strip()}
MAX_MSGS_PER_HOUR = int(os.getenv("MAX_MSGS_PER_HOUR", "30"))
MAX_MSGS_PER_DAY = int(os.getenv("MAX_MSGS_PER_DAY", "120"))
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"
GROUP_TRIGGER = os.getenv("GROUP_TRIGGER", "dibs").lower()

# Where concierge alerts go (your own phone number or Apple ID email).
OPERATOR_HANDLE = os.getenv("OPERATOR_HANDLE", "")

# Optional: email route for pay-on-arrival venues. A free Gmail account with an app password works.
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
BOOKING_REPLY_TO = os.getenv("BOOKING_REPLY_TO", "")

# Payments (optional). Keep the Stripe *test* key here until you are a registered business.
PAYMENTS_ENABLED = os.getenv("PAYMENTS_ENABLED", "0") == "1"
PAYMENTS_LIVE = os.getenv("PAYMENTS_LIVE", "0") == "1"
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
CURRENCY = os.getenv("DIBS_CURRENCY", "aud")
MAX_CHARGE_CENTS = int(os.getenv("MAX_CHARGE_CENTS", "30000"))
# The page shown after the card is saved (docs/card-saved.html, served by GitHub Pages). ?to= is the agent's address.
PAYMENT_RETURN_URL = os.getenv("PAYMENT_RETURN_URL", "https://arjunadelaide.github.io/dibs/card-saved.html?to=iamdibsagent@gmail.com")

# Free key from developer.ticketmaster.com. Without it, event search uses data/events.json only.
TICKETMASTER_API_KEY = os.getenv("TICKETMASTER_API_KEY", "")
COUNTRY_CODE = os.getenv("DIBS_COUNTRY", "AU")
EVENT_CHECK_HOURS = int(os.getenv("EVENT_CHECK_HOURS", "6"))

ALERT_INTERVAL_MINUTES = int(os.getenv("ALERT_INTERVAL_MINUTES", "30"))
# Sent once to each new person, after the first reply. Empty turns it off.
WELCOME_NOTE = os.getenv("WELCOME_NOTE", "PS: I'm Dibs, an AI agent. I find things to do, book them, and text you alerts you ask for. "
                         "I keep our messages so I can help you. Text STOP any time. "
                         "Privacy: https://arjunadelaide.github.io/dibs/privacy.html")

# Text "one sec" when a reply takes longer than this many seconds. 0 turns it off.
HOLDING_AFTER_SECONDS = float(os.getenv("HOLDING_AFTER_SECONDS", "8"))
# Phase A: open a browser window on this Mac with the booking prepared, for the operator to pay.
SUPERVISED_CHECKOUT = os.getenv("SUPERVISED_CHECKOUT", "0") == "1"
# The email address given to venues on bookings (their confirmations go here).
BOOKING_EMAIL = os.getenv("BOOKING_EMAIL", "")

# A paid booking that nobody completes in this time is refunded automatically.
PAID_TIMEOUT_MINUTES = int(os.getenv("PAID_TIMEOUT_MINUTES", "120"))
PROPOSAL_TTL_MINUTES = 30
HISTORY_TURNS = 20
MAX_TOOL_ROUNDS = 6
