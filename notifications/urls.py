from django.urls import path
from .views import (
    NotificationListView,
    NotificationUnreadCountView,
    MarkNotificationReadView,
    MarkAllNotificationsReadView
)

urlpatterns = [
    # GET: All notifications for the logged-in user, ordered by created_at desc, paginated
    path("", NotificationListView.as_view(), name="notification-list"),
    
    # GET: Returns {unread_count: N} for the logged-in user
    path("unread-count/", NotificationUnreadCountView.as_view(), name="notification-unread-count"),
    
    # PATCH: Marks a single notification as read
    path("<int:pk>/read/", MarkNotificationReadView.as_view(), name="mark-notification-read"),
    
    # POST: Marks all unread notifications for the logged-in user as read
    path("read-all/", MarkAllNotificationsReadView.as_view(), name="mark-all-notifications-read"),
]
