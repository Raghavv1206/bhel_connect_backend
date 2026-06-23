from django.contrib import admin
from adminpanel.admin import admin_site
from .models import Notification

@admin.register(Notification, site=admin_site)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('recipient', 'title', 'notification_type', 'is_read', 'created_at')
    list_filter = ('is_read', 'notification_type', 'created_at')
    search_fields = ('recipient__employee_id', 'recipient__name', 'title', 'message')
    ordering = ('-created_at',)
