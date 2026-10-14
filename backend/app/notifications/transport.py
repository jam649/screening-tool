"""Bounded TLS SMTP with no debug output, body logging or exception disclosure."""

import smtplib
import ssl
from email.message import EmailMessage

from app.notifications.settings import SMTPSettings


class DeliveryFailure(Exception):
    def __init__(self, code: str, *, uncertain: bool = False):
        self.code, self.uncertain = code, uncertain
        super().__init__(code)


def send_message(settings: SMTPSettings, *, recipient: str, payload: dict, message_id: str):
    if not settings.configured:
        raise DeliveryFailure("smtp_not_configured")
    message = EmailMessage()
    message["From"] = payload["sender"]
    message["To"] = recipient
    message["Subject"] = " ".join(payload["subject"].splitlines())
    message["Message-ID"] = message_id
    message.set_content(payload["text"])
    message.add_alternative(payload["html"], subtype="html")
    client: smtplib.SMTP | None = None
    try:
        context = ssl.create_default_context()
        if settings.tls == "ssl":
            client = smtplib.SMTP_SSL(settings.host, settings.port, timeout=15, context=context)
        else:
            client = smtplib.SMTP(settings.host, settings.port, timeout=15)
            client.ehlo()
            if settings.tls == "starttls":
                client.starttls(context=context)
                client.ehlo()
            elif settings.tls != "none" or settings.host not in {"localhost", "127.0.0.1", "::1"}:
                raise DeliveryFailure("smtp_tls_required")
        if settings.username:
            if settings.tls == "none":
                raise DeliveryFailure("smtp_tls_required")
            if not settings.password:
                raise DeliveryFailure("smtp_configuration_invalid")
            client.login(settings.username, settings.password)
        # Failure while DATA is in flight can mean the server already accepted it.
        try:
            refused = client.send_message(
                message, from_addr=payload["sender"], to_addrs=[recipient]
            )
            if refused:
                raise DeliveryFailure("recipient_rejected")
        except (
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPResponseException,
            smtplib.SMTPNotSupportedError,
        ):
            # SMTP protocol rejections inherit OSError, but their explicit
            # responses prove failure rather than an unknown delivery outcome.
            raise
        except (TimeoutError, smtplib.SMTPServerDisconnected, OSError):
            raise DeliveryFailure("delivery_uncertain", uncertain=True) from None
    except DeliveryFailure:
        raise
    except smtplib.SMTPAuthenticationError:
        raise DeliveryFailure("smtp_authentication_failed") from None
    except smtplib.SMTPRecipientsRefused:
        raise DeliveryFailure("recipient_rejected") from None
    except smtplib.SMTPSenderRefused:
        raise DeliveryFailure("sender_rejected") from None
    except smtplib.SMTPDataError:
        raise DeliveryFailure("smtp_data_rejected") from None
    except (smtplib.SMTPNotSupportedError, ssl.SSLError):
        raise DeliveryFailure("smtp_tls_or_feature_unavailable") from None
    except (smtplib.SMTPException, OSError, ValueError):
        raise DeliveryFailure("smtp_connection_failed") from None
    finally:
        if client is not None:
            # QUIT failures after successful DATA do not mean the message failed.
            try:
                client.quit()
            except Exception:
                client.close()
