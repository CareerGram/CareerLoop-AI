
import base64
import os
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


# We only need permission to send emails, not to read the inbox.
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
]

PROJECT_DIR = Path(__file__).resolve().parent

CREDENTIALS_FILE = PROJECT_DIR / os.getenv(
    "GOOGLE_CREDENTIALS_FILE",
    "credentials.json",
)

TOKEN_FILE = PROJECT_DIR / os.getenv(
    "GOOGLE_TOKEN_FILE",
    "token.json",
)


class GmailService:
    """Connect to Gmail and send a message when requested."""

    def __init__(self):
        credentials = None

        # Use the previous login if we already have a valid token.
        if TOKEN_FILE.exists():
            credentials = Credentials.from_authorized_user_file(
                str(TOKEN_FILE),
                GMAIL_SCOPES,
            )

        if not credentials or not credentials.valid:
            if (
                credentials
                and credentials.expired
                and credentials.refresh_token
            ):
                credentials.refresh(Request())

            else:
                if not CREDENTIALS_FILE.exists():
                    raise FileNotFoundError(
                        "credentials.json is missing. Download the OAuth "
                        "Desktop App credentials from Google Cloud and "
                        "place the file beside app.py."
                    )

                # Google opens a browser so you can sign in and approve access.
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(CREDENTIALS_FILE),
                    GMAIL_SCOPES,
                )
                credentials = flow.run_local_server(port=0)

            # Save the login token on this computer for the next run.
            TOKEN_FILE.write_text(
                credentials.to_json(),
                encoding="utf-8",
            )

        self.gmail = build(
            "gmail",
            "v1",
            credentials=credentials,
            cache_discovery=False,
        )

    @staticmethod
    def make_raw_message(recipient, subject, body):
        """Prepare the message in the format Gmail expects."""
        message = MIMEText(body, "plain", "utf-8")
        message["To"] = recipient
        message["Subject"] = subject

        return base64.urlsafe_b64encode(
            message.as_bytes()
        ).decode("utf-8")

    def send_email(self, recipient, subject, body):
        """Send one email and return the message ID from Gmail."""
        raw_message = self.make_raw_message(
            recipient,
            subject,
            body,
        )

        result = (
            self.gmail.users()
            .messages()
            .send(
                userId="me",
                body={"raw": raw_message},
            )
            .execute()
        )

        return result.get("id", "")
