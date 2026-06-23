import logging
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.core.files.storage import default_storage
from bhel_connect_backend.fields import LocalMediaResource, is_cloudinary_configured
from marketplace.models import MarketplaceListing
import cloudinary.uploader

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Delete all marketplace listings marked as sold more than 6 days ago, including their database records and Cloudinary images.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Display listings to be deleted without actually deleting them',
        )
        parser.add_argument(
            '--days',
            type=int,
            default=6,
            help='Number of days after which sold listings should be deleted (default is 6)',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        days = options['days']

        cutoff_date = timezone.now() - timedelta(days=days)
        listings = MarketplaceListing.objects.filter(status='sold', updated_at__lte=cutoff_date)

        count = listings.count()
        self.stdout.write(self.style.WARNING(f"Found {count} sold listing(s) updated before {cutoff_date}"))

        if dry_run:
            for listing in listings:
                self.stdout.write(f"Dry-run: Would delete listing ID {listing.id} - '{listing.title}' (updated_at: {listing.updated_at})")
                for img in listing.images.all():
                    self.stdout.write(f"  Dry-run: Would delete image: {img.image}")
            return

        deleted_count = 0
        for listing in listings:
            title = listing.title
            listing_id = listing.id
            self.stdout.write(f"Deleting listing ID {listing_id} - '{title}'...")

            # Delete images from Cloudinary or local storage
            for img in listing.images.all():
                image_field = img.image
                if not image_field:
                    continue

                if isinstance(image_field, LocalMediaResource):
                    try:
                        path = image_field.path
                        if default_storage.exists(path):
                            default_storage.delete(path)
                            self.stdout.write(self.style.SUCCESS(f"  Deleted local file: {path}"))
                        else:
                            self.stdout.write(f"  Local file does not exist: {path}")
                    except Exception as e:
                        self.stderr.write(self.style.ERROR(f"  Error deleting local file {image_field.path}: {e}"))
                else:
                    # Cloudinary storage
                    if is_cloudinary_configured() and hasattr(image_field, 'public_id'):
                        try:
                            public_id = image_field.public_id
                            if public_id:
                                result = cloudinary.uploader.destroy(public_id)
                                self.stdout.write(self.style.SUCCESS(f"  Deleted Cloudinary resource {public_id}: {result}"))
                            else:
                                self.stdout.write(f"  No public_id found for image: {image_field}")
                        except Exception as e:
                            self.stderr.write(self.style.ERROR(f"  Error deleting Cloudinary image {image_field.public_id}: {e}"))
                    else:
                        self.stdout.write(f"  Cloudinary not configured or image has no public_id. Skipping storage deletion.")

            # Delete the listing (cascade deletes details, chat messages, listing image DB records)
            try:
                listing.delete()
                deleted_count += 1
                self.stdout.write(self.style.SUCCESS(f"Successfully deleted listing ID {listing_id} - '{title}' and all associated DB data."))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"Failed to delete listing ID {listing_id} from database: {e}"))

        self.stdout.write(self.style.SUCCESS(f"Purge operation complete. Deleted {deleted_count} listing(s)."))
