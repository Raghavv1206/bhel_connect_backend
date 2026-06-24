from django.db import models
from django.conf import settings

class Notification(models.Model):
    """
    Represents user notifications for campaigns, marketplace activities, payments, chats, or system events.
    """
    NOTIFICATION_TYPES = [
        ('campaign', 'Campaign Update'),
        ('payment', 'Payment Update'),
        ('listing', 'Listing Moderation'),
        ('chat', 'New Chat Message'),
        ('system', 'System Notification'),
    ]

    # Recipient employee who will receive the notification
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
        db_index=True,
        help_text='Employee who receives this notification'
    )
    
    # Notification title (e.g. "Payment Approved")
    title = models.CharField(
        max_length=100,
        help_text='Brief title of the notification'
    )
    
    # Notification detail message
    message = models.CharField(
        max_length=500,
        help_text='Detailed content message of the notification'
    )
    
    # Flag to track whether the user has viewed this notification
    is_read = models.BooleanField(
        default=False,
        db_index=True,
        help_text='Indicates if the notification has been read by the user'
    )
    
    # Notification type for categorization and frontend icon rendering
    notification_type = models.CharField(
        max_length=20,
        choices=NOTIFICATION_TYPES,
        default='system',
        help_text='Categorization of the notification type'
    )
    
    # Optional deep link / path for redirecting user to the relevant page (e.g. /smartbuy/123)
    link = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text='Optional deep link/path (e.g., /smartbuy/12) for redirection on click'
    )
    
    # Timestamp when the notification was generated
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text='When this notification was created'
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Notification'
        verbose_name_plural = 'Notifications'
        indexes = [
            # Compound index for retrieving unread notifications for a user sorted by time
            models.Index(fields=['recipient', 'is_read', '-created_at'], name='idx_notif_user_unread'),
        ]

    def __str__(self):
        return f"Notification for {self.recipient.employee_id}: {self.title} ({'Read' if self.is_read else 'Unread'})"

    def save(self, *args, **kwargs):
        """
        Enforce a maximum cap of 30 notifications per recipient on creation.
        Deletes older notifications to prevent database bloat.
        """
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            max_notifications = 30
            # Retrieve the list of IDs of the 30 most recent notifications to keep
            recent_ids = list(
                Notification.objects.filter(recipient=self.recipient)
                .order_by('-created_at')
                .values_list('id', flat=True)[:max_notifications]
            )
            if recent_ids:
                Notification.objects.filter(recipient=self.recipient).exclude(id__in=recent_ids).delete()
