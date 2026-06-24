from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination
from django.shortcuts import get_object_or_404
from django.http import Http404

from .models import Notification
from .serializers import NotificationSerializer


class NotificationPagination(PageNumberPagination):
    """
    Pagination class specifically designed for notifications list.
    Page size: 20 per page as requested.
    """
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class NotificationListView(APIView):
    """
    GET: api/notifications/
    Returns a paginated list of notifications for the authenticated employee,
    ordered by creation date (newest first).
    """
    permission_classes = [IsAuthenticated]

    def perform_content_negotiation(self, request, force=False):
        # Bypass DRF format content negotiation to avoid 404 for format query params
        renderers = self.get_renderers()
        return (renderers[0], renderers[0].media_type)

    def get(self, request):
        try:
            notifications = Notification.objects.filter(recipient=request.user)
            paginator = NotificationPagination()
            page = paginator.paginate_queryset(notifications, request, view=self)
            
            if page is not None:
                serializer = NotificationSerializer(page, many=True)
                return paginator.get_paginated_response(serializer.data)
                
            serializer = NotificationSerializer(notifications, many=True)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("Failed to load notifications: %s", e, exc_info=True)
            return Response(
                {"detail": "An error occurred while loading notifications. Please try again later."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class NotificationUnreadCountView(APIView):
    """
    GET: api/notifications/unread-count/
    Returns the count of unread notifications for the logged-in user:
    { "unread_count": N }
    """
    permission_classes = [IsAuthenticated]

    def perform_content_negotiation(self, request, force=False):
        renderers = self.get_renderers()
        return (renderers[0], renderers[0].media_type)

    def get(self, request):
        try:
            count = Notification.objects.filter(recipient=request.user, is_read=False).count()
            return Response({"unread_count": count}, status=status.HTTP_200_OK)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("Failed to fetch unread count: %s", e, exc_info=True)
            return Response(
                {"detail": "An error occurred while fetching unread count. Please try again later."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class MarkNotificationReadView(APIView):
    """
    PATCH: api/notifications/<int:pk>/read/
    Marks a single notification as read.
    """
    permission_classes = [IsAuthenticated]

    def perform_content_negotiation(self, request, force=False):
        renderers = self.get_renderers()
        return (renderers[0], renderers[0].media_type)

    def patch(self, request, pk):
        try:
            notification = get_object_or_404(Notification, id=pk, recipient=request.user)
            # Set in memory to return in the serialized response, but delete from DB
            notification.is_read = True
            serializer = NotificationSerializer(notification)
            notification.delete()
            return Response(serializer.data, status=status.HTTP_200_OK)
        except Http404:
            return Response(
                {"detail": "Notification not found."},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("Failed to mark notification as read: %s", e, exc_info=True)
            return Response(
                {"detail": "An error occurred while marking notification as read. Please try again later."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class MarkAllNotificationsReadView(APIView):
    """
    POST: api/notifications/read-all/
    Marks all unread notifications for the logged-in user as read.
    """
    permission_classes = [IsAuthenticated]

    def perform_content_negotiation(self, request, force=False):
        renderers = self.get_renderers()
        return (renderers[0], renderers[0].media_type)

    def post(self, request):
        try:
            Notification.objects.filter(recipient=request.user).delete()
            return Response({"detail": "All notifications marked as read and cleared from database."}, status=status.HTTP_200_OK)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("Failed to update notifications: %s", e, exc_info=True)
            return Response(
                {"detail": "An error occurred while updating notifications. Please try again later."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
