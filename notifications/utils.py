import logging
from django.db import transaction
from .models import Notification

logger = logging.getLogger(__name__)


def create_notification(recipient, title, message, notification_type, link=None):
    """
    Creates a Notification for the specified recipient.
    If called within an active transaction block, defers execution until the transaction commits.
    This prevents database errors during notification creation from rolling back the main transaction.
    """
    def do_create():
        try:
            # Create notification inside a try-except to log any database-level exceptions silently
            return Notification.objects.create(
                recipient=recipient,
                title=title,
                message=message,
                notification_type=notification_type,
                link=link
            )
        except Exception as e:
            logger.error(f"Failed to create notification for {recipient.employee_id}: {e}", exc_info=True)
            return None

    # Check if there is an active transaction block
    connection = transaction.get_connection()
    if connection.in_atomic_block:
        transaction.on_commit(do_create)
        return None
    else:
        return do_create()


def create_notifications_bulk(notifications_list):
    """
    Creates multiple Notification objects in bulk.
    If called within an active transaction block, defers execution until the transaction commits.
    """
    def do_bulk_create():
        try:
            # Only bulk create if the list is not empty
            if notifications_list:
                return Notification.objects.bulk_create(notifications_list)
        except Exception as e:
            logger.error(f"Failed to bulk create notifications: {e}", exc_info=True)
            return []

    # Check if there is an active transaction block
    connection = transaction.get_connection()
    if connection.in_atomic_block:
        transaction.on_commit(do_bulk_create)
        return []
    else:
        return do_bulk_create() or []
