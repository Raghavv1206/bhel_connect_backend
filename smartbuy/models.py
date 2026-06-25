from datetime import datetime, timedelta
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.utils import timezone
from django.core.validators import MinValueValidator
from bhel_connect_backend.fields import CloudinaryField


class Vendor(models.Model):
    """
    Represents a vendor/supplier who provides products for SmartBuy campaigns.
    Vendors are managed by admins and linked to campaigns.
    """

    # Vendor company or business name
    name = models.CharField(
        max_length=200,
        help_text='Vendor company or business name'
    )

    # Primary contact person at the vendor
    contact_person = models.CharField(
        max_length=100,
        help_text='Primary contact person at the vendor'
    )

    # Vendor business email for communication
    email = models.EmailField(
        max_length=100,
        help_text='Vendor business email'
    )

    # Vendor phone number
    phone = models.CharField(
        max_length=15,
        help_text='Vendor contact phone number'
    )

    # Free-text description of products/services the vendor provides
    products_provided = models.TextField(
        help_text='Description of products/services this vendor supplies'
    )

    # Soft-delete flag — inactive vendors are hidden from campaign creation dropdowns
    is_active = models.BooleanField(
        default=True,
        help_text='Inactive vendors are hidden from new campaign creation'
    )

    # When this vendor record was created
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Vendor'
        verbose_name_plural = 'Vendors'

    def __str__(self):
        return self.name


class Campaign(models.Model):
    """
    Represents a SmartBuy group-buying campaign.
    Employees register to buy a product at volume-discounted prices.
    The price drops as more employees join (dynamic pricing via PricingTier).

    CRITICAL: Always use select_for_update() when modifying available_quantity
    to prevent race conditions during concurrent registrations.
    """

    # Campaign status choices
    STATUS_CHOICES = [
        ('active', 'Active'),         # Currently accepting registrations
        ('closed', 'Closed'),         # Successfully ended — final price locked
        ('cancelled', 'Cancelled'),   # Admin cancelled — all registrations refunded
    ]

    # Campaign title displayed to employees (e.g., "Dell Latitude 5540 Laptop")
    title = models.CharField(
        max_length=200,
        help_text='Campaign title shown to employees'
    )

    # Detailed product/campaign description with specs, terms, etc.
    description = models.TextField(
        help_text='Detailed product description, specifications, and terms'
    )

    # Vendor supplying this product — SET_NULL so campaign data survives vendor deletion
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.SET_NULL,
        null=True,
        related_name='campaigns',
        db_index=True,
        help_text='Vendor supplying this product'
    )

    # Product image stored on Cloudinary — shown on campaign cards and detail pages
    product_image = CloudinaryField(
        'image',
        folder='bhel/campaigns',
        blank=True,
        null=True,
        help_text='Product image for campaign display'
    )

    # Total quantity of units available in this campaign
    total_quantity = models.PositiveIntegerField(
        help_text='Total units available in this campaign'
    )

    # Remaining available units — decremented atomically on registration
    # CRITICAL: Use select_for_update() when decrementing to prevent overselling
    available_quantity = models.PositiveIntegerField(
        help_text='Remaining units — use select_for_update() when modifying'
    )

    # Campaign duration in days from start_date
    duration_days: int = models.PositiveIntegerField(
        help_text='Campaign duration in days from start_date'
    )

    # Campaign start date — set by admin on creation
    start_date: datetime = models.DateTimeField(
        help_text='When the campaign starts accepting registrations'
    )

    # Campaign end date — auto-calculated as start_date + duration_days
    end_date: datetime = models.DateTimeField(
        help_text='Auto-calculated: start_date + duration_days'
    )

    # Current campaign lifecycle status
    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default='active',
        db_index=True,
        help_text='Campaign lifecycle status'
    )

    # UPI QR code image for manual payment (fallback if Cashfree is not used)
    upi_qr_image = CloudinaryField(
        'image',
        folder='bhel/qr_codes',
        blank=True,
        null=True,
        help_text='UPI QR code image for manual payment fallback'
    )

    # Custom token deposit amount required to join this campaign
    token_deposit = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text='Custom token deposit amount required to join this campaign'
    )

    # Custom refund amount returned to the employee upon cancellation
    cancellation_refund_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text='Custom refund amount returned to the employee upon cancellation during active phase'
    )

    # Admin who created this campaign — PROTECT prevents accidental admin deletion
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='created_campaigns',
        db_index=True,
        help_text='Admin employee who created this campaign'
    )

    # Record creation timestamp
    created_at = models.DateTimeField(auto_now_add=True)

    # Last modification timestamp
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Campaign'
        verbose_name_plural = 'Campaigns'
        indexes = [
            # Fast lookups for active campaigns list and auto-close checks
            models.Index(fields=['status', '-created_at'], name='idx_campaign_status_date'),
            models.Index(fields=['status', 'end_date'], name='idx_campaign_status_end'),
        ]

    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        """Auto-calculate end_date from start_date + duration_days if not explicitly set."""
        if self.start_date and self.duration_days:
            calculated_end = self.start_date + timedelta(days=self.duration_days)
            # Only auto-set if end_date is not yet set or if start/duration changed
            if not self.end_date or self.end_date != calculated_end:
                self.end_date = calculated_end
        super().save(*args, **kwargs)

    @property
    def confirmed_buyers_count(self):
        """Count of registrations with approved payment (confirmed buyers)."""
        if hasattr(self, 'annotated_confirmed_buyers_count'):
            return self.annotated_confirmed_buyers_count
        return self.registrations.filter(payment_status='approved').count()

    @property
    def total_registrations_count(self):
        """Count of all non-cancelled registrations (includes pending payments)."""
        return self.registrations.exclude(payment_status='cancelled').count()

    @property
    def waitlisted_count(self):
        """Count of employees currently on the waitlist."""
        return self.registrations.filter(is_waitlisted=True).count()

    def get_current_price(self):
        """
        Determine current price based on confirmed buyer count and pricing tiers.
        Finds the PricingTier where min_buyers <= confirmed_count <= max_buyers.
        Falls back to the first tier's price if count is below minimum threshold.
        Applies Django cache with 30-second TTL to optimize database load.
        """
        from django.core.cache import cache
        cache_key = f"campaign_price_{self.id}"
        cached_price = cache.get(cache_key)
        if cached_price is not None:
            return cached_price

        confirmed = self.confirmed_buyers_count
        
        # Optimize: Iterate over prefetched pricing_tiers in memory to avoid N+1 query loops
        tiers = list(self.pricing_tiers.all())
        matching_tier = None
        for tier in tiers:
            if tier.min_buyers <= confirmed and (tier.max_buyers is None or tier.max_buyers >= confirmed):
                matching_tier = tier
                break

        if matching_tier:
            price = matching_tier.price
        else:
            # Fallback: if buyer count is below the first tier's minimum, use the first tier price
            # Sort tiers in memory to avoid another query
            sorted_tiers = sorted(tiers, key=lambda t: t.min_buyers)
            price = sorted_tiers[0].price if sorted_tiers else None

        if price is not None:
            cache.set(cache_key, price, 30)  # 30-second TTL
        return price

    @property
    def is_active(self):
        """Campaign is active if status is 'active' and end_date has not passed."""
        return self.status == 'active' and self.end_date > timezone.now()

    @property
    def is_sold_out(self):
        """Campaign is sold out when no more units are available."""
        return self.available_quantity == 0

    @property
    def time_remaining(self):
        """Time remaining until campaign ends. Returns timedelta or None if ended."""
        if self.end_date > timezone.now():
            return self.end_date - timezone.now()
        return None


class PricingTier(models.Model):
    """
    Defines a price tier for a campaign based on the number of confirmed buyers.
    Multiple tiers create a step-function pricing curve:
    e.g., 1-10 buyers = ₹5000, 11-25 buyers = ₹4700, 26+ buyers = ₹4500

    Tiers must be non-overlapping and contiguous — validated in the serializer.
    """

    # The campaign this pricing tier belongs to
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name='pricing_tiers',
        db_index=True,
        help_text='Campaign this tier belongs to'
    )

    # Minimum number of confirmed buyers for this tier to activate
    min_buyers = models.PositiveIntegerField(
        help_text='Minimum confirmed buyers for this price tier'
    )

    # Maximum number of confirmed buyers for this tier (null = unlimited/no cap)
    max_buyers = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text='Maximum confirmed buyers for this tier — null means no upper limit'
    )

    # Price per unit when this tier is active
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text='Price per unit for this buyer range'
    )

    class Meta:
        ordering = ['min_buyers']
        verbose_name = 'Pricing Tier'
        verbose_name_plural = 'Pricing Tiers'
        # Ensure no two tiers for the same campaign have the same min_buyers
        unique_together = ('campaign', 'min_buyers')

    def __str__(self):
        max_display = self.max_buyers if self.max_buyers else '∞'
        return f"{self.campaign.title}: {self.min_buyers}-{max_display} buyers @ ₹{self.price}"


class CampaignRegistration(models.Model):
    """
    Records an employee's registration for a SmartBuy campaign.
    Handles both confirmed slots and waitlist positions.

    CRITICAL: Creation must be wrapped in transaction.atomic() with
    select_for_update() on the Campaign to prevent race conditions on
    available_quantity.
    """

    # Payment status choices
    PAYMENT_STATUS_CHOICES = [
        ('pending', 'Pending'),       # Registered but payment not yet confirmed
        ('approved', 'Approved'),     # Payment verified — slot confirmed
        ('rejected', 'Rejected'),     # Payment rejected — slot released
        ('cancelled', 'Cancelled'),   # Registration cancelled by employee or system
    ]

    # Refund status choices
    REFUND_STATUS_CHOICES = [
        ('not_applicable', 'Not Applicable'),  # No refund needed
        ('pending', 'Pending'),                # Refund due but not yet processed
        ('processed', 'Processed'),            # Refund completed
    ]

    # The campaign this registration is for
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name='registrations',
        db_index=True,
        help_text='Campaign the employee registered for'
    )

    # The employee who registered
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='campaign_registrations',
        db_index=True,
        help_text='Employee who registered for this campaign'
    )

    # When the registration was made
    reservation_date = models.DateTimeField(
        auto_now_add=True,
        help_text='When the employee registered'
    )

    # Token amount paid/to be paid (matches the custom campaign token deposit)
    token_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text='Token amount paid/to be paid for this registration — immutable once set'
    )

    # Current payment status
    payment_status = models.CharField(
        max_length=10,
        choices=PAYMENT_STATUS_CHOICES,
        default='pending',
        db_index=True,
        help_text='Current payment status'
    )

    # Cashfree payment gateway order ID — set when payment order is created
    cashfree_order_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text='Cashfree payment gateway order identifier'
    )

    # Cashfree payment ID — set when payment is confirmed via webhook
    cashfree_payment_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text='Cashfree payment identifier — set on webhook confirmation'
    )

    # Admin who approved/rejected the payment (for manual approval flow)
    payment_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_registrations',
        help_text='Admin who approved/rejected the payment'
    )

    # When the payment was approved/rejected
    payment_approved_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Timestamp of payment approval/rejection'
    )

    # UPI payment screenshot for manual payment confirmation fallback
    upi_screenshot = CloudinaryField(
        'image',
        folder='bhel/payments',
        blank=True,
        null=True,
        help_text='Screenshot of UPI payment deposit'
    )

    # Deadline to confirm slot reservation when promoted from waitlist (24 hours)
    slot_expiry_date = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Deadline to confirm reservation when promoted from waitlist'
    )

    # Whether this registration is on the waitlist (True) or has a confirmed slot (False)
    is_waitlisted = models.BooleanField(
        default=False,
        db_index=True,
        help_text='True if employee is waitlisted (no slot available at registration time)'
    )

    # Position in the waitlist queue (null if not waitlisted)
    waitlist_position = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text='Queue position in waitlist — null if not waitlisted'
    )

    # When the employee cancelled their registration (null if not cancelled)
    cancellation_date = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the employee cancelled — null if not cancelled'
    )

    # Refund amount due on cancellation (calculated based on backout timing)
    refund_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Refund amount calculated based on cancellation timing'
    )

    # Refund processing status
    refund_status = models.CharField(
        max_length=15,
        choices=REFUND_STATUS_CHOICES,
        default='not_applicable',
        help_text='Current refund processing status'
    )

    class Meta:
        ordering = ['-reservation_date']
        verbose_name = 'Campaign Registration'
        verbose_name_plural = 'Campaign Registrations'
        # DB-level constraint: one registration per employee per campaign — prevents double-booking
        unique_together = ('campaign', 'employee')
        indexes = [
            # Fast lookups for admin payment queue
            models.Index(fields=['campaign', 'payment_status'], name='idx_reg_campaign_payment'),
            # Fast lookups for waitlist ordering
            models.Index(fields=['campaign', 'is_waitlisted', 'waitlist_position'], name='idx_reg_waitlist'),
        ]

    def __str__(self):
        status = 'Waitlisted' if self.is_waitlisted else self.get_payment_status_display()
        return f"{self.employee.name} → {self.campaign.title} ({status})"


class TokenPayment(models.Model):
    """
    Tracks the token payment (advance deposit) for a campaign registration.
    Integrates with Cashfree payment gateway for automated verification.
    One-to-one with CampaignRegistration — each registration has at most one payment.
    """

    # Payment status choices
    STATUS_CHOICES = [
        ('pending', 'Pending'),     # Payment initiated but not yet confirmed
        ('approved', 'Approved'),   # Payment confirmed (via Cashfree webhook or admin)
        ('rejected', 'Rejected'),   # Payment rejected (dispute or failed verification)
    ]

    # The registration this payment is for — OneToOne ensures one payment per registration
    registration = models.OneToOneField(
        CampaignRegistration,
        on_delete=models.CASCADE,
        related_name='token_payment',
        help_text='The campaign registration this payment covers'
    )

    # Payment amount in INR
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text='Token payment amount in INR'
    )

    # Cashfree order ID — unique identifier from payment gateway
    cashfree_order_id = models.CharField(
        max_length=64,
        unique=True,
        help_text='Unique Cashfree order identifier'
    )

    # Cashfree payment ID — set when payment is completed
    cashfree_payment_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text='Cashfree payment ID — set on successful payment'
    )

    # When the payment was submitted/initiated
    submitted_at = models.DateTimeField(
        auto_now_add=True,
        help_text='When the payment was initiated'
    )

    # Current payment verification status
    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default='pending',
        db_index=True,
        help_text='Payment verification status'
    )

    # Admin who reviewed this payment (for manual review cases)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_payments',
        help_text='Admin who reviewed this payment'
    )

    # When the payment was reviewed
    reviewed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the payment was reviewed by admin'
    )

    # Reason for rejection (required when status is 'rejected')
    rejection_reason = models.TextField(
        null=True,
        blank=True,
        help_text='Required explanation when rejecting a payment'
    )

    class Meta:
        ordering = ['-submitted_at']
        verbose_name = 'Token Payment'
        verbose_name_plural = 'Token Payments'
        indexes = [
            # Fast lookups for admin payment approval queue
            models.Index(fields=['status', '-submitted_at'], name='idx_payment_status_date'),
        ]

    def __str__(self):
        return f"₹{self.amount} payment for {self.registration} ({self.get_status_display()})"


from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

@receiver(post_save, sender=CampaignRegistration)
@receiver(post_delete, sender=CampaignRegistration)
def invalidate_price_cache_on_registration_change(sender, instance, **kwargs):
    from django.core.cache import cache
    cache_key = f"campaign_price_{instance.campaign_id}"
    cache.delete(cache_key)
