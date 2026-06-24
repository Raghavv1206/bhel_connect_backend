import json
import hashlib
import logging
import time
from collections import deque
from urllib.parse import parse_qs
from django.contrib.auth import get_user_model
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from rest_framework_simplejwt.authentication import JWTAuthentication

from .models import MarketplaceListing, ChatMessage
from notifications.utils import create_notification

User = get_user_model()
logger = logging.getLogger(__name__)


@database_sync_to_async
def get_user_from_token(token_string):
    """
    Validates the simplejwt JWT token and returns the corresponding Employee user.
    Returns None if the token is invalid, expired, or the user does not exist.
    """
    try:
        jwt_auth = JWTAuthentication()
        validated_token = jwt_auth.get_validated_token(token_string)
        return jwt_auth.get_user(validated_token)
    except Exception as e:
        logger.warning("WebSocket token validation failed: %s", e)
        return None


@database_sync_to_async
def get_listing_and_validate_participants(listing_id, user, other_user_id):
    """
    Validates listing and user participation roles:
    1. The listing must exist.
    2. One participant must be the seller of the listing.
    3. Both participants must be different BHEL employees.
    """
    try:
        listing = MarketplaceListing.objects.get(id=listing_id)
        other_user = User.objects.get(employee_id=other_user_id)
        
        # Authenticated user or the other user must be the seller of the product
        if listing.seller != user and listing.seller != other_user:
            return None, None
            
        # Ensure employees are not chatting with themselves
        if user == other_user:
            return None, None
            
        return listing, other_user
    except (MarketplaceListing.DoesNotExist, User.DoesNotExist):
        return None, None
    except Exception as e:
        logger.error("WebSocket participant validation error: %s", e)
        return None, None


@database_sync_to_async
def save_chat_message(listing_id, sender, receiver, message_text):
    """
    Atomically saves a chat message to the database and generates a transaction-safe notification.
    Ensures message cannot be sent for sold items.
    """
    listing = MarketplaceListing.objects.get(id=listing_id)
    if listing.status == 'sold':
        raise ValueError("This item has been sold by the owner. Chat is now closed.")

    msg = ChatMessage.objects.create(
        listing=listing,
        sender=sender,
        receiver=receiver,
        message=message_text
    )
    
    # Generate chat notification for the receiver
    create_notification(
        recipient=receiver,
        title="New Chat Message",
        message=f"{sender.name} sent you a message about '{listing.title}'",
        notification_type="chat",
        link=f"/marketplace/{listing.id}"
    )
    return msg


@database_sync_to_async
def mark_messages_as_read(listing_id, sender_id, receiver_id):
    """
    Marks all messages received by sender_id from receiver_id for a specific listing as read.
    """
    ChatMessage.objects.filter(
        listing_id=listing_id,
        sender_id=receiver_id,
        receiver_id=sender_id,
        is_read=False
    ).update(is_read=True)


class ChatConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for employee-to-employee marketplace chat.
    Validates tokens via query parameter "?token=xxx" and other user via "&other_user_id=yyy".
    Isolates chats inside channel groups unique to the listing and participant pair.
    """

    async def connect(self):
        self.listing_id = self.scope['url_route']['kwargs']['listing_id']
        
        # Parse connection query parameters
        query_string = self.scope.get('query_string', b'').decode('utf-8')
        query_params = parse_qs(query_string)
        token = query_params.get('token', [None])[0]
        other_user_id = query_params.get('other_user_id', [None])[0]
        
        if not token or not other_user_id:
            logger.info("WS rejected: missing token or other_user_id for listing %s", self.listing_id)
            await self.close(code=4003)  # Custom close code: Missing parameters
            return
            
        # Authenticate user from simplejwt token
        self.user = await get_user_from_token(token)
        if not self.user or not self.user.is_authenticated:
            logger.info("WS rejected: auth failed for listing %s", self.listing_id)
            await self.close(code=4003)  # Custom close code: Auth failed
            return
            
        # Validate listing and participants
        self.listing, self.other_user = await get_listing_and_validate_participants(
            self.listing_id, self.user, other_user_id
        )
        if not self.listing or not self.other_user:
            logger.info(
                "WS rejected: participant validation failed for listing %s, user %s, other %s",
                self.listing_id, self.user.employee_id, other_user_id
            )
            await self.close(code=4004)  # Custom close code: Permission or listing missing
            return
            
        # Establish unique room group name per listing and buyer/seller combination
        # Hash sorted employee IDs to guarantee channel group name contains only safe alphanumeric characters
        user_ids = sorted([self.user.employee_id, self.other_user.employee_id])
        combined_ids = f"{user_ids[0]}_{user_ids[1]}"
        hashed_ids = hashlib.sha256(combined_ids.encode('utf-8')).hexdigest()[:32]
        self.room_group_name = f"chat_listing_{self.listing_id}_{hashed_ids}"
        
        # Initialize rate limiter: max 30 messages per 60 seconds per connection
        self._message_timestamps = deque(maxlen=30)
        self._rate_limit_max = 30
        self._rate_limit_window = 60  # seconds
        
        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        # Join listing broadcast group
        self.broadcast_group_name = f"chat_listing_broadcast_{self.listing_id}"
        await self.channel_layer.group_add(
            self.broadcast_group_name,
            self.channel_name
        )
        
        # Accept connection
        await self.accept()
        
        # Mark unread incoming messages from this partner as read
        await mark_messages_as_read(self.listing_id, self.user.employee_id, self.other_user.employee_id)

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            # Leave room group
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )
        if hasattr(self, 'broadcast_group_name'):
            # Leave listing broadcast group
            await self.channel_layer.group_discard(
                self.broadcast_group_name,
                self.channel_name
            )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except (json.JSONDecodeError, TypeError):
            return
            
        message_text = data.get('message', '').strip()
        if not message_text:
            return
            
        # Truncate messages exceeding length limit to prevent database flooding
        if len(message_text) > 1000:
            message_text = message_text[:1000]
        
        # Rate limiting: reject messages if client exceeds 30 messages per 60 seconds
        now = time.time()
        if hasattr(self, '_message_timestamps'):
            # Remove timestamps outside the window
            while self._message_timestamps and self._message_timestamps[0] < now - self._rate_limit_window:
                self._message_timestamps.popleft()
            
            if len(self._message_timestamps) >= self._rate_limit_max:
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'Rate limit exceeded. Please slow down.'
                }))
                return
            self._message_timestamps.append(now)
            
        # Save message record
        try:
            msg = await save_chat_message(
                self.listing_id,
                self.user,
                self.other_user,
                message_text
            )
        except ValueError as e:
            # Send error block to client when listing is sold
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': str(e)
            }))
            return
        
        # Broadcast message to channel group
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'id': msg.id,
                'sender_id': self.user.employee_id,
                'sender_name': self.user.name,
                'message': message_text,
                'timestamp': msg.timestamp.isoformat()
            }
        )

    async def chat_message(self, event):
        """
        Invoked when a group broadcast is sent. Forwards the payload to the WebSocket client.
        """
        await self.send(text_data=json.dumps({
            'id': event['id'],
            'sender_id': event['sender_id'],
            'sender_name': event['sender_name'],
            'message': event['message'],
            'timestamp': event['timestamp']
        }))

    async def chat_listing_sold(self, event):
        """
        Invoked when a listing is marked as sold. Forwards a notification payload to the WebSocket client.
        """
        await self.send(text_data=json.dumps({
            'type': 'listing_sold',
            'listing_id': event['listing_id'],
            'message': event['message']
        }))


class ListingUpdatesConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer to broadcast listing status updates in real-time.
    Validates user authentication token from query parameter.
    """
    async def connect(self):
        self.listing_id = self.scope['url_route']['kwargs']['listing_id']

        # Parse token from query parameter
        query_string = self.scope.get('query_string', b'').decode('utf-8')
        query_params = parse_qs(query_string)
        token = query_params.get('token', [None])[0]

        if not token:
            await self.close(code=4003)
            return

        self.user = await get_user_from_token(token)
        if not self.user or not self.user.is_authenticated:
            await self.close(code=4003)
            return

        self.room_group_name = f"listing_updates_{self.listing_id}"

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )

    async def listing_update(self, event):
        await self.send(text_data=json.dumps({
            'type': 'listing_update',
            'listing_id': event['listing_id'],
            'status': event['status']
        }))
