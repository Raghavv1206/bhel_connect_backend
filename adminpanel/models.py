from django.db import models
from django.conf import settings

class AuditLog(models.Model):
    """
    Represents an audit log entry for actions performed by admin users.
    Captures administrative actions for security tracking, accountability, and debugging.
    """
    # The admin employee who performed the action
    admin_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='admin_audit_logs',
        db_index=True,
        help_text='The admin employee who performed this action'
    )
    
    # HTTP Method or logical action type (e.g. POST, PUT, DELETE, BULK_IMPORT)
    action = models.CharField(
        max_length=50,
        help_text='HTTP method or logic action name (e.g., POST, DELETE, BULK_IMPORT)'
    )
    
    # Target model name that was affected (e.g. "Campaign", "MarketplaceListing")
    target_model = models.CharField(
        max_length=100,
        help_text='Name of the target database model affected'
    )
    
    # Primary Key of the target model instance that was affected
    target_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text='Primary key of the affected model instance'
    )
    
    # Human-readable detailed description of the action and changes
    description = models.TextField(
        help_text='Human-readable detailed description of the administrative action'
    )
    
    # Timestamp when the action was performed
    timestamp = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text='When the action was performed'
    )
    
    # IP Address of the admin who performed the action
    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text='IP address of the admin user'
    )

    class Meta:
        ordering = ['-timestamp']
        verbose_name = 'Audit Log'
        verbose_name_plural = 'Audit Logs'
        indexes = [
            # Compound index for filtering audit logs by admin user and time
            models.Index(fields=['admin_user', '-timestamp'], name='idx_audit_admin_time'),
            # Index for looking up activity related to a specific target object
            models.Index(fields=['target_model', 'target_id'], name='idx_audit_target_lookup'),
        ]

    def __str__(self):
        return f"{self.admin_user.employee_id} - {self.action} {self.target_model} (ID: {self.target_id}) at {self.timestamp}"
