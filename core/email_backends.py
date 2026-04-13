import logging

from django.core.mail.backends.base import BaseEmailBackend
import requests

logger = logging.getLogger(__name__)


class ResendEmailBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        import resend
        from django.conf import settings

        self.resend = resend
        self.api_key = settings.RESEND_API_KEY
        self.default_from = settings.DEFAULT_FROM_EMAIL
        self.resend.api_key = self.api_key

    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        sent_count = 0
        for message in email_messages:
            try:
                from_email = message.from_email or self.default_from
                to_list = list(message.to or [])
                if not from_email or not to_list:
                    continue

                html_body = message.alternatives[0][0] if getattr(message, "alternatives", None) else None
                payload = {
                    "from": from_email,
                    "to": to_list,
                    "subject": message.subject or "",
                }
                if html_body:
                    payload["html"] = html_body
                    if message.body:
                        payload["text"] = message.body
                else:
                    payload["text"] = message.body or ""

                self.resend.Emails.send(payload)
                sent_count += 1
            except Exception:
                logger.exception("Resend send failed for recipients=%s", getattr(message, "to", []))
                if not self.fail_silently:
                    raise
        return sent_count


class BrevoAPIEmailBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        from django.conf import settings

        self.api_key = settings.BREVO_API_KEY
        self.default_from = settings.DEFAULT_FROM_EMAIL
        self.timeout = getattr(settings, "EMAIL_TIMEOUT", 5)

    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        if not self.api_key:
            logger.warning("BREVO_API_KEY missing; cannot send emails via API.")
            return 0

        sent_count = 0
        headers = {
            "accept": "application/json",
            "api-key": self.api_key,
            "content-type": "application/json",
        }
        endpoint = "https://api.brevo.com/v3/smtp/email"

        for message in email_messages:
            try:
                from_email = message.from_email or self.default_from
                to_list = [{"email": e} for e in (message.to or []) if e]
                if not from_email or not to_list:
                    continue

                html_body = message.alternatives[0][0] if getattr(message, "alternatives", None) else None
                payload = {
                    "sender": {"email": from_email},
                    "to": to_list,
                    "subject": message.subject or "",
                }
                if html_body:
                    payload["htmlContent"] = html_body
                    if message.body:
                        payload["textContent"] = message.body
                else:
                    payload["textContent"] = message.body or ""

                resp = requests.post(endpoint, headers=headers, json=payload, timeout=self.timeout)
                if resp.status_code >= 400:
                    raise RuntimeError(f"Brevo API error {resp.status_code}: {resp.text}")
                sent_count += 1
            except Exception:
                logger.exception("Brevo API send failed for recipients=%s", getattr(message, "to", []))
                if not self.fail_silently:
                    raise
        return sent_count
