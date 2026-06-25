from django.contrib import admin
from adminpanel.admin import admin_site
from .models import Category, MarketplaceListing, ListingImage, VehicleListing, PropertyListing, ChatMessage

class ListingImageInline(admin.TabularInline):
    """
    Inline admin setup to manage photos directly inside the parent MarketplaceListing view.
    """
    model = ListingImage
    extra = 1
    fields = ('image', 'is_primary', 'uploaded_at')
    readonly_fields = ('uploaded_at',)


class VehicleListingInline(admin.StackedInline):
    """
    Inline admin setup to edit vehicle specifications on the same page as the MarketplaceListing.
    """
    model = VehicleListing
    can_delete = False
    verbose_name = 'Vehicle Specification'
    verbose_name_plural = 'Vehicle Specifications'


class PropertyListingInline(admin.StackedInline):
    """
    Inline admin setup to edit property details on the same page as the MarketplaceListing.
    """
    model = PropertyListing
    can_delete = False
    verbose_name = 'Property Detail'
    verbose_name_plural = 'Property Details'


@admin.register(Category, site=admin_site)
class CategoryAdmin(admin.ModelAdmin):
    """
    Admin control for Category hierarchy.
    """
    list_display = ('name', 'slug', 'parent', 'display_order')
    prepopulated_fields = {'slug': ('name',)}
    list_filter = ('parent',)
    search_fields = ('name', 'slug')
    ordering = ('display_order', 'name')


@admin.register(MarketplaceListing, site=admin_site)
class MarketplaceListingAdmin(admin.ModelAdmin):
    """
    Admin control for Marketplace Listings.
    Allows quick approval, rejection, and specifications viewing.
    """
    list_display = ('title', 'seller', 'price', 'condition', 'category', 'status', 'expires_at', 'views', 'created_at')
    list_filter = ('status', 'condition', 'category', 'created_at')
    search_fields = ('title', 'description', 'seller__employee_id', 'seller__name')
    inlines = [ListingImageInline, VehicleListingInline, PropertyListingInline]
    readonly_fields = ('views', 'created_at', 'updated_at')
    actions = ['approve_listings', 'reject_listings']

    def approve_listings(self, request, queryset):
        from django.utils import timezone
        from datetime import timedelta
        for listing in queryset:
            listing.status = 'available'
            listing.expires_at = timezone.now() + timedelta(days=30)
            listing.save()
    approve_listings.short_description = "Approve selected listings (make available)"

    def reject_listings(self, request, queryset):
        queryset.update(status='rejected')
    reject_listings.short_description = "Reject selected listings"


@admin.register(ChatMessage, site=admin_site)
class ChatMessageAdmin(admin.ModelAdmin):
    """
    Read-only admin logger for employee communications to prevent moderation abuse.
    """
    list_display = ('sender', 'receiver', 'listing', 'message_preview', 'timestamp', 'is_read')
    list_filter = ('is_read', 'timestamp')
    search_fields = ('sender__name', 'receiver__name', 'message', 'listing__title')
    readonly_fields = ('sender', 'receiver', 'listing', 'message', 'timestamp')

    def message_preview(self, obj):
        return obj.message[:50] + '...' if len(obj.message) > 50 else obj.message
    message_preview.short_description = 'Message'
