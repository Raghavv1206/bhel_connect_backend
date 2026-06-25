import os
import logging
import threading
from decimal import Decimal
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger(__name__)


def send_smartbuy_email_async(subject, text_template, html_template, context, recipient_list, attachments=None):
    """
    Renders email templates and dispatches the email using Django's EmailMultiAlternatives
    inside a background thread to prevent SMTP latency from blocking the main request thread.
    """
    if not recipient_list:
        return

    try:
        text_content = render_to_string(text_template, context)
        html_content = render_to_string(html_template, context)
    except Exception as e:
        logger.error(f"Failed to render email templates for {text_template}/{html_template}: {e}", exc_info=True)
        return

    def _send():
        try:
            from_email = settings.DEFAULT_FROM_EMAIL
            msg = EmailMultiAlternatives(subject, text_content, from_email, recipient_list)
            msg.attach_alternative(html_content, "text/html")
            if attachments:
                for filename, content, mimetype in attachments:
                    msg.attach(filename, content, mimetype)
            msg.send(fail_silently=False)
            logger.info(f"Successfully sent email '{subject}' to {recipient_list}")
        except Exception as err:
            logger.error(f"Failed to send email '{subject}' to {recipient_list}: {err}", exc_info=True)

    # If running tests, dispatch synchronously to avoid race conditions with assertions
    import sys
    is_testing = 'test' in sys.argv or getattr(settings, 'TESTING', False)
    if is_testing:
        _send()
        return

    thread = threading.Thread(target=_send)
    thread.daemon = True
    thread.start()


def get_frontend_url():
    return os.environ.get('FRONTEND_URL', 'http://localhost:5173').rstrip('/')


def send_order_initiated_email(registration):
    """Disabled to conserve daily email limits. Logged only."""
    logger.debug(f"Email skipped: order initiated for registration {registration.id}")
    return


def send_payment_confirmed_email(registration):
    """Sent on successful token deposit payment confirmation."""
    frontend_url = get_frontend_url()
    campaign = registration.campaign
    employee = registration.employee

    # Safely retrieve payment information from related TokenPayment
    order_id = registration.cashfree_order_id
    payment_id = registration.cashfree_payment_id
    amount = registration.token_amount

    try:
        if hasattr(registration, 'token_payment') and registration.token_payment:
            order_id = registration.token_payment.cashfree_order_id or order_id
            payment_id = registration.token_payment.cashfree_payment_id or payment_id
            amount = registration.token_payment.amount or amount
    except Exception:
        pass

    context = {
        'user': employee,
        'campaign': campaign,
        'order_id': order_id,
        'payment_id': payment_id,
        'amount': amount,
        'date': timezone.now(),
        'campaign_link': f"{frontend_url}/smartbuy/{campaign.id}",
        'subject': f"BHEL Connect - Booking Confirmed for {campaign.title}"
    }

    send_smartbuy_email_async(
        subject=context['subject'],
        text_template='emails/payment_confirmed.txt',
        html_template='emails/payment_confirmed.html',
        context=context,
        recipient_list=[employee.email]
    )


def send_payment_failed_email(registration):
    """Sent when Cashfree token deposit payment fails."""
    frontend_url = get_frontend_url()
    campaign = registration.campaign
    employee = registration.employee

    order_id = registration.cashfree_order_id
    amount = registration.token_amount

    try:
        if hasattr(registration, 'token_payment') and registration.token_payment:
            order_id = registration.token_payment.cashfree_order_id or order_id
            amount = registration.token_payment.amount or amount
    except Exception:
        pass

    context = {
        'user': employee,
        'campaign': campaign,
        'order_id': order_id,
        'amount': amount,
        'date': timezone.now(),
        'campaign_link': f"{frontend_url}/smartbuy/{campaign.id}",
        'subject': f"BHEL Connect - Token Payment Failed for {campaign.title}"
    }

    send_smartbuy_email_async(
        subject=context['subject'],
        text_template='emails/payment_failed.txt',
        html_template='emails/payment_failed.html',
        context=context,
        recipient_list=[employee.email]
    )


def send_waitlist_joined_email(registration):
    """Disabled to conserve daily email limits. Logged only."""
    logger.debug(f"Email skipped: waitlist joined for registration {registration.id}")
    return


def send_waitlist_promoted_email(registration):
    """Disabled to conserve daily email limits. Logged only."""
    logger.debug(f"Email skipped: waitlist promoted for registration {registration.id}")
    return


def send_slot_expired_email(registration):
    """Disabled to conserve daily email limits. Logged only."""
    logger.debug(f"Email skipped: slot expired for registration {registration.id}")
    return


def send_registration_cancelled_email(registration):
    """Sent when a registration is cancelled."""
    frontend_url = get_frontend_url()
    campaign = registration.campaign
    employee = registration.employee

    context = {
        'user': employee,
        'campaign': campaign,
        'refund_amount': registration.refund_amount or Decimal('0.00'),
        'refund_status': registration.refund_status,
        'date': registration.cancellation_date or timezone.now(),
        'campaign_link': f"{frontend_url}/smartbuy/{campaign.id}",
        'subject': f"BHEL Connect - Registration Cancelled for {campaign.title}"
    }

    send_smartbuy_email_async(
        subject=context['subject'],
        text_template='emails/registration_cancelled.txt',
        html_template='emails/registration_cancelled.html',
        context=context,
        recipient_list=[employee.email]
    )


def send_campaign_closed_email(campaign, registrations):
    """Disabled to conserve daily email limits. Logged only."""
    logger.debug(f"Email skipped: campaign closed for campaign {campaign.id}")
    return


def send_campaign_cancelled_email(campaign, registrations):
    """Disabled to conserve daily email limits. Logged only."""
    logger.debug(f"Email skipped: campaign cancelled for campaign {campaign.id}")
    return


def send_campaign_report_to_admin(campaign):
    """Disabled to conserve daily email limits. Logged only."""
    logger.debug(f"Email skipped: campaign report for campaign {campaign.id}")
    return
