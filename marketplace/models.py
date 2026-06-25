from decimal import Decimal
from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from bhel_connect_backend.fields import CloudinaryField


class Category(models.Model):
    """
    Hierarchical product category for the marketplace.
    Supports two-level nesting: parent categories contain subcategories.
    e.g., 'Electronics' → 'Laptops', 'Phones'
          'Vehicles' → 'Cars', 'Bikes'

    Slugs are used in URL filtering (e.g., /marketplace?category=electronics).
    """

    # Category display name
    name = models.CharField(
        max_length=100,
        help_text='Category display name (e.g., Electronics, Vehicles)'
    )

    # Self-referencing FK for parent-child hierarchy — null means top-level category
    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='children',
        db_index=True,
        help_text='Parent category — null for top-level categories'
    )

    # URL-safe slug used for filtering (e.g., "electronics", "two-wheelers")
    slug = models.SlugField(
        max_length=100,
        unique=True,
        help_text='URL-safe identifier for filtering — must be unique'
    )

    # Controls display ordering in dropdown menus and category lists
    display_order = models.IntegerField(
        default=0,
        help_text='Lower numbers display first in category lists'
    )

    class Meta:
        ordering = ['display_order', 'name']
        verbose_name = 'Category'
        verbose_name_plural = 'Categories'

    def __str__(self):
        if self.parent:
            return f"{self.parent.name} → {self.name}"
        return self.name

    @property
    def is_parent(self):
        """True if this category has child subcategories."""
        return self.children.exists()


class MarketplaceListing(models.Model):
    """
    Represents an item listed for sale by an employee in the internal marketplace.
    Listings go through admin moderation before becoming visible to other employees.

    Status flow: pending → approved/rejected → available → reserved → sold
    """

    # Listing status choices — represents the lifecycle of a listing
    STATUS_CHOICES = [
        ('pending', 'Pending Review'),     # Awaiting admin moderation
        ('approved', 'Approved'),          # Admin approved — visible to buyers
        ('available', 'Available'),        # Approved and actively listed
        ('reserved', 'Reserved'),          # Seller marked as reserved for a buyer
        ('sold', 'Sold'),                  # Transaction completed
        ('rejected', 'Rejected'),          # Admin rejected — not visible
        ('expired', 'Expired'),            # Listing has expired after 1 month
    ]

    # Item condition choices
    CONDITION_CHOICES = [
        ('new', 'New'),                    # Brand new, unused
        ('like_new', 'Like New'),          # Used briefly, excellent condition
        ('good', 'Good'),                  # Normal wear, fully functional
        ('fair', 'Fair'),                  # Noticeable wear, still functional
    ]

    # Employee selling this item
    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='marketplace_listings',
        db_index=True,
        help_text='Employee selling this item'
    )

    # Listing title — concise product name (e.g., "iPhone 15 Pro 256GB")
    title = models.CharField(
        max_length=100,
        help_text='Concise listing title — max 100 characters'
    )

    # Detailed product description with condition details, accessories, etc.
    description = models.TextField(
        max_length=2000,
        help_text='Detailed description — max 2000 characters'
    )

    # Asking price in INR
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('1'))],
        help_text='Asking price in INR — minimum ₹1'
    )

    # Physical condition of the item
    condition = models.CharField(
        max_length=10,
        choices=CONDITION_CHOICES,
        help_text='Physical condition of the item'
    )

    # Product category for filtering and browsing
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        related_name='listings',
        db_index=True,
        help_text='Product category for browsing and filtering'
    )

    # Moderation status — controls visibility and lifecycle
    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default='pending',
        db_index=True,
        help_text='Listing moderation and lifecycle status'
    )

    # Admin rejection reason — required when status is 'rejected'
    rejection_reason = models.TextField(
        null=True,
        blank=True,
        help_text='Required when admin rejects — explains why to the seller'
    )

    # View counter — incremented server-side on detail page access
    views = models.PositiveIntegerField(
        default=0,
        help_text='Number of times the listing detail page was viewed'
    )

    # Record timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text='DateTime when the listing expires and becomes inactive'
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Marketplace Listing'
        verbose_name_plural = 'Marketplace Listings'
        indexes = [
            # Primary browsing query: approved/available listings sorted by newest
            models.Index(fields=['status', '-created_at'], name='idx_listing_status_date'),
            # Price-range filtering
            models.Index(fields=['status', 'price'], name='idx_listing_status_price'),
            # Category browsing
            models.Index(fields=['category', 'status'], name='idx_listing_category_status'),
            # Seller's own listings
            models.Index(fields=['seller', 'status'], name='idx_listing_seller_status'),
        ]

    def __str__(self):
        return f"{self.title} — ₹{self.price} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        """
        Override save to set default expires_at if not specified.
        Defaults to 30 days from creation.
        """
        if not self.expires_at:
            from django.utils import timezone
            from datetime import timedelta
            # Use created_at if already set, else now
            base_time = self.created_at or timezone.now()
            self.expires_at = base_time + timedelta(days=30)
        super().save(*args, **kwargs)

    @property
    def is_expired(self):
        """
        True if the listing has expired dynamically or has an 'expired' status.
        """
        if self.status == 'expired':
            return True
        from django.utils import timezone
        if self.status in ['available', 'reserved'] and self.expires_at and self.expires_at < timezone.now():
            return True
        return False

    def increment_views(self, user):
        """
        Atomically increment the view counter using F() to prevent race conditions
        only if this is a unique profile view by a user (other than the seller).
        """
        if not user or not user.is_authenticated:
            return

        # Prevent seller from inflating their own views
        if user == self.seller:
            return

        from django.db import transaction, IntegrityError
        from django.db.models import F
        try:
            # Try to record a view for this listing and user profile inside a transaction savepoint
            with transaction.atomic():
                MarketplaceListingView.objects.create(listing=self, user=user)
            # If successful, atomically increment views on the MarketplaceListing
            MarketplaceListing.objects.filter(pk=self.pk).update(views=F('views') + 1)
        except IntegrityError:
            # Profile has already viewed this listing, do not increment
            pass


class MarketplaceListingView(models.Model):
    """
    Tracks unique profile views of a marketplace listing.
    Prevents multiple views from the same user profile from inflating the view counter.
    """

    # The listing this view belongs to
    listing = models.ForeignKey(
        MarketplaceListing,
        on_delete=models.CASCADE,
        related_name='profile_views',
        db_index=True,
        help_text='The marketplace listing that was viewed'
    )

    # The employee/user who viewed this listing
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='viewed_marketplace_listings',
        db_index=True,
        help_text='The employee who viewed the listing'
    )

    # Timestamp when the listing was viewed
    viewed_at = models.DateTimeField(
        auto_now_add=True,
        help_text='Timestamp of the unique view'
    )

    class Meta:
        verbose_name = 'Marketplace Listing View'
        verbose_name_plural = 'Marketplace Listing Views'
        constraints = [
            models.UniqueConstraint(fields=['listing', 'user'], name='unique_listing_user_view')
        ]
        indexes = [
            models.Index(fields=['listing', 'user']),
        ]

    def __str__(self):
        return f"{self.user} viewed {self.listing.title}"


class ListingImage(models.Model):
    """
    Stores images for a marketplace listing.
    Each listing can have up to 5 images (enforced in the serializer).
    Images are stored on Cloudinary — fast CDN delivery with zero server load.
    """

    # The listing this image belongs to
    listing = models.ForeignKey(
        MarketplaceListing,
        on_delete=models.CASCADE,
        related_name='images',
        db_index=True,
        help_text='The listing this image belongs to'
    )

    # Image file stored on Cloudinary CDN
    image = CloudinaryField(
        'image',
        folder='bhel/marketplace',
        help_text='Listing photo stored on Cloudinary CDN'
    )

    # Whether this is the primary/cover image shown in list views
    is_primary = models.BooleanField(
        default=False,
        help_text='Primary image shown in listing cards and search results'
    )

    # When this image was uploaded
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-is_primary', 'uploaded_at']
        verbose_name = 'Listing Image'
        verbose_name_plural = 'Listing Images'

    def __str__(self):
        primary_tag = ' [PRIMARY]' if self.is_primary else ''
        return f"Image for {self.listing.title}{primary_tag}"


class VehicleListing(models.Model):
    """
    Extended fields for vehicle listings (cars, bikes, etc.).
    One-to-one relationship with MarketplaceListing — only created for vehicle category items.
    """

    # Fuel type choices
    FUEL_CHOICES = [
        ('petrol', 'Petrol'),
        ('diesel', 'Diesel'),
        ('electric', 'Electric'),
        ('cng', 'CNG'),
    ]

    # Transmission type choices
    TRANSMISSION_CHOICES = [
        ('manual', 'Manual'),
        ('automatic', 'Automatic'),
    ]

    # Link to the parent listing — OneToOne ensures one vehicle record per listing
    listing = models.OneToOneField(
        MarketplaceListing,
        on_delete=models.CASCADE,
        related_name='vehicle_details',
        help_text='Parent marketplace listing'
    )

    # Vehicle make/brand (e.g., Maruti, Honda, Hyundai)
    brand = models.CharField(
        max_length=50,
        help_text='Vehicle make/brand (e.g., Maruti, Honda)'
    )

    # Vehicle model name (e.g., Swift, City, Creta)
    model = models.CharField(
        max_length=50,
        help_text='Vehicle model name (e.g., Swift, City)'
    )

    # Manufacturing year
    year = models.PositiveIntegerField(
        validators=[MinValueValidator(1990), MaxValueValidator(2030)],
        help_text='Manufacturing year (1990-2030)'
    )

    # Odometer reading in kilometers
    km_driven = models.PositiveIntegerField(
        help_text='Total kilometers driven'
    )

    # Fuel type
    fuel_type = models.CharField(
        max_length=10,
        choices=FUEL_CHOICES,
        help_text='Vehicle fuel type'
    )

    # Transmission type
    transmission = models.CharField(
        max_length=10,
        choices=TRANSMISSION_CHOICES,
        help_text='Vehicle transmission type'
    )

    class Meta:
        verbose_name = 'Vehicle Listing Details'
        verbose_name_plural = 'Vehicle Listing Details'

    def __str__(self):
        return f"{self.year} {self.brand} {self.model}"


class PropertyListing(models.Model):
    """
    Extended fields for property listings (apartments, houses, plots, etc.).
    One-to-one relationship with MarketplaceListing — only created for property category items.
    """

    # Property type choices
    PROPERTY_TYPE_CHOICES = [
        ('apartment', 'Apartment'),
        ('house', 'Independent House'),
        ('villa', 'Villa'),
        ('plot', 'Plot/Land'),
        ('commercial', 'Commercial'),
    ]

    # Listing type — sale or rent
    LISTING_TYPE_CHOICES = [
        ('sale', 'For Sale'),
        ('rent', 'For Rent'),
    ]

    # Link to the parent listing
    listing = models.OneToOneField(
        MarketplaceListing,
        on_delete=models.CASCADE,
        related_name='property_details',
        help_text='Parent marketplace listing'
    )

    # Property location/area (e.g., "Bhopal, MP — near BHEL Township")
    location = models.CharField(
        max_length=200,
        help_text='Property location/area description'
    )

    # Total area in square feet
    area_sqft = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('1'))],
        help_text='Total area in square feet'
    )

    # Number of bedrooms (0 for plots/commercial)
    bedrooms = models.PositiveIntegerField(
        default=0,
        help_text='Number of bedrooms — 0 for plots and commercial'
    )

    # Number of bathrooms (0 for plots)
    bathrooms = models.PositiveIntegerField(
        default=0,
        help_text='Number of bathrooms — 0 for plots'
    )

    # Type of property
    property_type = models.CharField(
        max_length=15,
        choices=PROPERTY_TYPE_CHOICES,
        help_text='Type of property'
    )

    # Whether property is for sale or rent
    listing_type = models.CharField(
        max_length=5,
        choices=LISTING_TYPE_CHOICES,
        help_text='For sale or for rent'
    )

    class Meta:
        verbose_name = 'Property Listing Details'
        verbose_name_plural = 'Property Listing Details'

    def __str__(self):
        return f"{self.get_property_type_display()} in {self.location} ({self.area_sqft} sqft)"


class ChatMessage(models.Model):
    """
    Stores chat messages between buyer and seller for a marketplace listing.
    Messages are persisted via the WebSocket consumer (Django Channels) and
    loaded via REST API for chat history.

    Access control: Only the buyer and seller of a listing can exchange messages.
    """

    # The listing being discussed
    listing = models.ForeignKey(
        MarketplaceListing,
        on_delete=models.CASCADE,
        related_name='chat_messages',
        db_index=True,
        help_text='The listing this conversation is about'
    )

    # Employee who sent the message
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sent_messages',
        db_index=True,
        help_text='Employee who sent the message'
    )

    # Employee who receives the message
    receiver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='received_messages',
        db_index=True,
        help_text='Employee who receives the message'
    )

    # Message text content — max 1000 chars to prevent abuse
    message = models.TextField(
        max_length=1000,
        help_text='Message content — max 1000 characters'
    )

    # When the message was sent
    timestamp = models.DateTimeField(
        auto_now_add=True,
        help_text='When the message was sent'
    )

    # Whether the receiver has seen this message
    is_read = models.BooleanField(
        default=False,
        db_index=True,
        help_text='Whether the receiver has read this message'
    )

    class Meta:
        ordering = ['timestamp']
        verbose_name = 'Chat Message'
        verbose_name_plural = 'Chat Messages'
        indexes = [
            # Fast message history lookup per listing conversation
            models.Index(fields=['listing', 'timestamp'], name='idx_chat_listing_time'),
            # Unread message count queries
            models.Index(fields=['receiver', 'is_read'], name='idx_chat_receiver_read'),
            # Conversation lookup between two users for a listing
            models.Index(fields=['listing', 'sender', 'receiver'], name='idx_chat_conversation'),
        ]

    def __str__(self):
        message_str = str(self.message)
        preview = message_str[:50] + '...' if len(message_str) > 50 else message_str
        return f"{self.sender.name} → {self.receiver.name}: {preview}"
