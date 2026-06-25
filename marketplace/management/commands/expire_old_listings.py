import logging
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from marketplace.models import MarketplaceListing
from notifications.utils import create_notification

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Automatically transition active marketplace listings that have passed their expiration date to the expired status.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview the listings that would be expired without modifying the database',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        now = timezone.now()

        # Query listings that are available or reserved, and whose expires_at is in the past
        expired_listings = MarketplaceListing.objects.filter(
            status__in=['available', 'reserved'],
            expires_at__lte=now
        ).select_related('seller')

        count = expired_listings.count()
        self.stdout.write(self.style.WARNING(f"Found {count} listing(s) that have expired (cutoff: {now})"))

        if dry_run:
            for listing in expired_listings:
                self.stdout.write(
                    f"Dry-run: Would expire listing ID {listing.id} - '{listing.title}' (expires_at: {listing.expires_at}, current status: {listing.status})"
                )
            return

        expired_count = 0
        channel_layer = get_channel_layer()

        for listing in expired_listings:
            title = listing.title
            listing_id = listing.id
            seller = listing.seller
            self.stdout.write(f"Expiring listing ID {listing_id} - '{title}'...")

            try:
                # Execute in atomic transaction to guarantee status update and notification creation
                with transaction.atomic():
                    listing.status = 'expired'
                    listing.save(update_fields=['status', 'updated_at'])

                    # Generate notification for seller
                    create_notification(
                        recipient=seller,
                        title="Listing Expired",
                        message=f"Your marketplace listing for '{title}' has expired after 1 month. If it is not sold, you must create a new ad.",
                        notification_type="listing",
                        link="/profile?tab=mylistings"
                    )

                # Broadcast WebSocket status change to active listing detail connections
                if channel_layer:
                    try:
                        async_to_sync(channel_layer.group_send)(
                            f"listing_updates_{listing_id}",
                            {
                                "type": "listing_update",
                                "status": "expired",
                                "listing_id": listing_id
                            }
                        )
                    except Exception as ws_err:
                        self.stderr.write(self.style.ERROR(f"  WebSocket broadcast failed for listing {listing_id}: {ws_err}"))

                expired_count += 1
                self.stdout.write(self.style.SUCCESS(f"Successfully expired listing ID {listing_id} - '{title}' and notified seller."))

            except Exception as e:
                self.stderr.write(self.style.ERROR(f"Failed to expire listing ID {listing_id} due to error: {e}"))

        self.stdout.write(self.style.SUCCESS(f"Expiration sweep complete. Expired {expired_count} listing(s)."))
