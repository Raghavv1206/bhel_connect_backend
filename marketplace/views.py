from django.shortcuts import get_object_or_404
from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.decorators import action
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.views import APIView
from rest_framework.pagination import PageNumberPagination

from django.db.models import Q, Count, Max
from users.permissions import IsAdminEmployee, IsOwnerOrAdmin, IsOwnerOnly
from notifications.utils import create_notification
from .models import Category, MarketplaceListing, ChatMessage
from .serializers import CategorySerializer, MarketplaceListingSerializer
from .filters import ListingFilter


class CategoryViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Category management.
    """
    queryset = Category.objects.all().order_by('display_order', 'name')
    serializer_class = CategorySerializer

    def get_permissions(self):
        """
        Only allow authenticated users to view categories, and admins to manage them.
        """
        if self.action in ['list', 'retrieve']:
            return [IsAuthenticated()]
        return [IsAdminEmployee()]


class MarketplaceListingPagination(PageNumberPagination):
    page_size = 12
    page_size_query_param = 'page_size'
    max_page_size = 100


class MarketplaceListingViewSet(viewsets.ModelViewSet):
    """
    ViewSet for browsing and managing marketplace listings.
    By default, lists approved ('available') listings. Admins can view all states.
    Uses select_related and prefetch_related to eliminate N+1 queries.
    """
    queryset = MarketplaceListing.objects.select_related('seller', 'category', 'vehicle_details', 'property_details').prefetch_related('images').filter(status='available').order_by('-created_at')
    serializer_class = MarketplaceListingSerializer
    pagination_class = MarketplaceListingPagination
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = ListingFilter

    def get_permissions(self):
        """
        Dynamically resolve permission classes based on view actions.
        """
        if self.action in ['update', 'partial_update', 'destroy']:
            return [IsAuthenticated(), IsOwnerOrAdmin()]
        return super().get_permissions()

    def get_queryset(self):
        """
        Restrict queries based on user privileges.
        Admins can view all listings. Owners can see their own listings of any status.
        Regular users only see available/reserved (non-expired) and sold items.
        """
        user = self.request.user
        queryset = MarketplaceListing.objects.select_related('seller', 'category', 'vehicle_details', 'property_details')
        
        if user.is_authenticated:
            # Prefetch only the current user's saved products to avoid N+1 queries
            from users.models import SavedProduct
            from django.db.models import Prefetch
            user_saved_queryset = SavedProduct.objects.filter(employee=user)
            queryset = queryset.prefetch_related(
                'images',
                Prefetch('saved_by', queryset=user_saved_queryset, to_attr='user_saved')
            )
            
            if user.is_admin:
                return queryset.order_by('-created_at')
            else:
                from django.db.models import Q
                from django.utils import timezone
                now = timezone.now()
                # Regular users see active (available/reserved) listings that haven't expired, plus sold ones, plus their own listings
                return queryset.filter(
                    Q(status__in=['available', 'reserved'], expires_at__gt=now) |
                    Q(status='sold') |
                    Q(seller=user)
                ).order_by('-created_at')
        else:
            queryset = queryset.prefetch_related('images')
            
        return queryset.none()

    def filter_queryset(self, queryset):
        """
        Only apply the filterset class (ListingFilter) to the list action.
        This prevents detail / status-update actions from returning 404 due to list-level status filters.
        """
        if self.action == 'list':
            return super().filter_queryset(queryset)
        return queryset

    def perform_create(self, serializer):
        serializer.save(seller=self.request.user)

    def retrieve(self, request, *args, **kwargs):
        """
        Retrieves a listing detail and atomically increments its view count based on unique profiles.
        """
        instance = self.get_object()
        instance.increment_views(request.user)
        instance.refresh_from_db()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def update(self, request, *args, **kwargs):
        """
        Overrides update to verify that the listing is not already sold or expired,
        and reverts the status to 'pending' if updated by a non-admin owner.
        """
        instance = self.get_object()
        if instance.status == 'sold' or instance.is_expired:
            return Response(
                {"detail": "Cannot modify details of a listing that has already been marked as sold or is expired."}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        response = super().update(request, *args, **kwargs)
        
        # If updated by owner (non-admin), revert status to pending review
        if response.status_code == status.HTTP_200_OK:
            instance.refresh_from_db()
            if not request.user.is_admin:
                instance.status = 'pending'
                instance.save()
            # Broadcast the updated status (reverted to pending or admin-modified status)
            self._broadcast_status_change(instance.id, instance.status)
            if not request.user.is_admin:
                # Re-serialize with updated status
                serializer = self.get_serializer(instance)
                return Response(serializer.data)
            
        return response

    @action(detail=True, methods=['patch'], url_path='status', permission_classes=[IsAuthenticated, IsOwnerOnly])
    def update_status(self, request, pk=None):
        """
        PATCH: api/marketplace/listings/:id/status/
        Allows listing owners to transition statuses (available -> reserved -> sold).
        """
        listing = self.get_object()
        new_status = request.data.get('status')

        if new_status not in ['available', 'reserved', 'sold']:
            return Response(
                {"detail": "Invalid status value. Permissible transitions are: available, reserved, sold."}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Security check: Prevent owners from activating pending or rejected listings
        if listing.status in ['pending', 'rejected'] and new_status != 'sold':
            return Response(
                {"detail": "Cannot modify status of a listing that is pending review or rejected, except to mark it as sold."}, 
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Security check: Prevent modifying sold or expired listings
        if listing.status == 'sold' or listing.is_expired:
            return Response(
                {"detail": "Cannot modify status of a listing that has already been marked as sold or is expired."}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        listing.status = new_status
        listing.save()
        self._broadcast_status_change(listing.id, new_status)
        if new_status == 'sold':
            self._broadcast_listing_sold(listing.id)
        serializer = self.get_serializer(listing)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='pending', permission_classes=[IsAuthenticated, IsAdminEmployee])
    def pending_listings(self, request):
        """
        GET: api/marketplace/listings/pending/
        Retrieves all pending listings awaiting administrative review (Admin only).
        """
        queryset = MarketplaceListing.objects.select_related('seller', 'category', 'vehicle_details', 'property_details').prefetch_related('images').filter(status='pending').order_by('-created_at')
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='approve', permission_classes=[IsAuthenticated, IsAdminEmployee])
    def approve_listing(self, request, pk=None):
        """
        POST: api/marketplace/listings/:id/approve/
        Approves a pending listing, changing its status to available and notifying the seller (Admin only).
        """
        listing = self.get_object()
        if listing.status != 'pending':
            return Response(
                {"detail": "Only pending listings can be approved."}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        listing.status = 'available'
        from django.utils import timezone
        from datetime import timedelta
        listing.expires_at = timezone.now() + timedelta(days=30)
        listing.save()
        self._broadcast_status_change(listing.id, 'available')

        # Generate notification for seller
        create_notification(
            recipient=listing.seller,
            title="Listing Approved!",
            message=f"Your marketplace listing for '{listing.title}' has been approved and is now active.",
            notification_type="listing",
            link=f"/marketplace/{listing.id}"
        )

        return Response({"detail": "Listing approved successfully."}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='reject', permission_classes=[IsAuthenticated, IsAdminEmployee])
    def reject_listing(self, request, pk=None):
        """
        POST: api/marketplace/listings/:id/reject/
        Rejects a pending listing with a detailed reason, notifying the seller (Admin only).
        """
        listing = self.get_object()
        if listing.status != 'pending':
            return Response(
                {"detail": "Only pending listings can be rejected."}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        reason = request.data.get('rejection_reason')
        if not reason or not reason.strip():
            return Response(
                {"detail": "rejection_reason is required to reject a listing."}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        listing.status = 'rejected'
        listing.rejection_reason = reason.strip()
        listing.save()
        self._broadcast_status_change(listing.id, 'rejected')

        # Generate notification for seller
        create_notification(
            recipient=listing.seller,
            title="Listing Rejected",
            message=f"Your marketplace listing for '{listing.title}' was rejected. Reason: {reason}",
            notification_type="listing",
            link="/profile"
        )

        return Response({"detail": "Listing rejected successfully."}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['delete'], url_path='remove', permission_classes=[IsAuthenticated, IsAdminEmployee])
    def remove_listing(self, request, pk=None):
        """
        DELETE: api/marketplace/listings/:id/remove/
        Performs administrative hard deletion / listing removal (Admin only).
        """
        listing = self.get_object()
        listing_id = listing.id
        listing.delete()
        self._broadcast_status_change(listing_id, 'deleted')
        return Response({"detail": "Listing deleted successfully from marketplace."}, status=status.HTTP_200_OK)

    def _broadcast_status_change(self, listing_id, status_value):
        """
        Helper method to broadcast real-time listing updates (status changes)
        over Django Channels WebSocket group.
        """
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f"listing_updates_{listing_id}",
                {
                    "type": "listing_update",
                    "status": status_value,
                    "listing_id": listing_id
                }
            )

    def _broadcast_listing_sold(self, listing_id):
        """
        Helper method to broadcast a listing sold notification to all active
        chat sessions (buyer/seller groups) for this listing.
        """
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f"chat_listing_broadcast_{listing_id}",
                {
                    "type": "chat_listing_sold",
                    "listing_id": listing_id,
                    "message": "This item has been sold by the owner. Chat is now closed."
                }
            )


class ChatConversationsView(APIView):
    """
    GET: api/marketplace/chats/
    Retrieves all distinct active conversations for the authenticated employee,
    along with listing summary, other participant details, last message, and unread counts.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        
        # Query unread counts grouped by (listing_id, sender_id) in a single query
        unread_counts = ChatMessage.objects.filter(
            receiver=user,
            is_read=False
        ).values('listing_id', 'sender_id').annotate(count=Count('id'))
        
        unread_dict = {(item['listing_id'], item['sender_id']): item['count'] for item in unread_counts}

        # For messages user sent: group by listing and receiver, get max message ID
        sent_latest = ChatMessage.objects.filter(
            sender=user
        ).values('listing_id', 'receiver_id').annotate(max_id=Max('id'))
        
        # For messages user received: group by listing and sender, get max message ID
        received_latest = ChatMessage.objects.filter(
            receiver=user
        ).values('listing_id', 'sender_id').annotate(max_id=Max('id'))
        
        # Group and find the absolute latest message ID for each (listing_id, other_user_id) conversation
        latest_ids = {}
        for item in sent_latest:
            if not item['listing_id']:
                continue
            key = (item['listing_id'], item['receiver_id'])
            latest_ids[key] = max(latest_ids.get(key, 0), item['max_id'])
            
        for item in received_latest:
            if not item['listing_id']:
                continue
            key = (item['listing_id'], item['sender_id'])
            latest_ids[key] = max(latest_ids.get(key, 0), item['max_id'])
            
        message_ids = list(latest_ids.values())

        # Retrieve only the latest message per conversation, prefetching relations
        messages = ChatMessage.objects.filter(
            id__in=message_ids
        ).select_related(
            'listing', 'listing__seller', 'sender', 'receiver'
        ).prefetch_related(
            'listing__images'
        ).order_by('-timestamp')

        conversations = []

        for msg in messages:
            # Skip messages with deleted listings
            if not msg.listing:
                continue
                
            other_user = msg.receiver if msg.sender == user else msg.sender
            
            # Fetch cover photo url if available in memory from prefetched list to avoid N+1 queries
            cover_image_url = None
            listing_images = list(msg.listing.images.all())
            if listing_images:
                # Find primary image in memory
                primary = next((img for img in listing_images if img.is_primary), None)
                cover_image_url = primary.image.url if primary else listing_images[0].image.url

            conversations.append({
                "id": msg.id,
                "listing": {
                    "id": msg.listing.id,
                    "title": msg.listing.title,
                    "price": str(msg.listing.price),
                    "status": msg.listing.status,
                    "cover_image": cover_image_url,
                    "seller_id": msg.listing.seller.employee_id
                },
                "other_user": {
                    "employee_id": other_user.employee_id,
                    "name": other_user.name,
                    "department": other_user.department,
                    "profile_picture": other_user.profile_picture.url if other_user.profile_picture else None
                },
                "last_message": {
                    "message": msg.message,
                    "timestamp": msg.timestamp,
                    "sender_id": msg.sender.employee_id
                },
                "unread_count": unread_dict.get((msg.listing_id, other_user.employee_id), 0)
            })

        return Response(conversations, status=status.HTTP_200_OK)


class ChatMessageHistoryView(APIView):
    """
    GET: api/marketplace/chats/<int:listing_id>/messages/
    Fetches paginated chat message history (default 50 messages from end)
    between authenticated user and other_user_id for a listing.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, listing_id):
        user = request.user
        other_user_id = request.query_params.get('other_user_id')
        if not other_user_id:
            return Response(
                {"detail": "other_user_id query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        listing = get_object_or_404(MarketplaceListing, id=listing_id)

        # Access control: User must be the seller, or the other_user must be the seller.
        # This ensures only buyer-seller conversations are accessible.
        if listing.seller != user and listing.seller.employee_id != other_user_id:
            return Response(
                {"detail": "You do not have permission to access this chat conversation."},
                status=status.HTTP_403_FORBIDDEN
            )

        # Query messages between participants for this listing
        messages = ChatMessage.objects.filter(
            Q(listing_id=listing_id),
            (Q(sender=user, receiver_id=other_user_id) | Q(sender_id=other_user_id, receiver=user))
        ).select_related('sender', 'receiver').order_by('timestamp')

        # Safe integer parsing with defaults and caps to prevent abuse
        try:
            limit = min(int(request.query_params.get('limit', 50)), 100)  # Cap at 100
            limit = max(limit, 1)  # Minimum 1
        except (ValueError, TypeError):
            limit = 50
        try:
            offset = max(int(request.query_params.get('offset', 0)), 0)
        except (ValueError, TypeError):
            offset = 0
        total_count = messages.count()

        # Fetch messages sliced from the end (newest first in terms of offset)
        messages_sliced = messages[max(0, total_count - offset - limit):total_count - offset] if total_count > offset else []

        # Mark all incoming messages in this window as read
        ChatMessage.objects.filter(
            listing_id=listing_id,
            sender_id=other_user_id,
            receiver=user,
            is_read=False
        ).update(is_read=True)

        data = []
        for msg in messages_sliced:
            data.append({
                "id": msg.id,
                "sender_id": msg.sender.employee_id,
                "sender_name": msg.sender.name,
                "receiver_id": msg.receiver.employee_id,
                "receiver_name": msg.receiver.name,
                "message": msg.message,
                "timestamp": msg.timestamp,
                "is_read": msg.is_read
            })

        return Response({
            "count": total_count,
            "results": data,
            "listing_status": listing.status
        }, status=status.HTTP_200_OK)
