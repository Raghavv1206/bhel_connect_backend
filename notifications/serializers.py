from rest_framework import serializers
from .models import Notification

class NotificationSerializer(serializers.ModelSerializer):
    """
    Serializer for the Notification model.
    Only allows updating is_read; all other fields are read-only.
    """
    class Meta:
        model = Notification
        fields = [
            'id', 
            'recipient', 
            'title', 
            'message', 
            'is_read', 
            'notification_type', 
            'link', 
            'created_at'
        ]
        read_only_fields = [
            'id', 
            'recipient', 
            'title', 
            'message', 
            'notification_type', 
            'link', 
            'created_at'
        ]
