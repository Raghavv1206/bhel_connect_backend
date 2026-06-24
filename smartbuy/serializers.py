from decimal import Decimal
from rest_framework import serializers
from .models import Vendor, Campaign, PricingTier, CampaignRegistration, TokenPayment

class VendorSerializer(serializers.ModelSerializer):
    """
    Serializer for the Vendor model.
    """
    class Meta:
        model = Vendor
        fields = ['id', 'name', 'contact_person', 'email', 'phone', 'products_provided', 'is_active', 'created_at']
        read_only_fields = ['id', 'created_at']


class PricingTierSerializer(serializers.ModelSerializer):
    """
    Serializer for PricingTiers.
    """
    class Meta:
        model = PricingTier
        fields = ['id', 'min_buyers', 'max_buyers', 'price']


class CampaignSerializer(serializers.ModelSerializer):
    """
    Serializer for SmartBuy campaigns.
    Includes nested pricing tiers and computed pricing fields.
    """
    pricing_tiers = PricingTierSerializer(many=True)
    vendor_name = serializers.CharField(source='vendor.name', read_only=True)
    current_price = serializers.SerializerMethodField()
    confirmed_buyers_count = serializers.IntegerField(read_only=True)
    is_sold_out = serializers.BooleanField(read_only=True)
    time_remaining_seconds = serializers.SerializerMethodField()
    user_registration = serializers.SerializerMethodField()

    class Meta:
        model = Campaign
        fields = [
            'id', 'title', 'description', 'vendor', 'vendor_name', 'product_image', 
            'total_quantity', 'available_quantity', 'duration_days', 'start_date', 
            'end_date', 'status', 'upi_qr_image', 'created_by', 'created_at', 
            'pricing_tiers', 'current_price', 'confirmed_buyers_count', 'is_sold_out',
            'time_remaining_seconds', 'user_registration', 'token_deposit', 'cancellation_refund_amount'
        ]
        read_only_fields = ['id', 'end_date', 'created_by', 'created_at', 'available_quantity']

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if instance.product_image:
            representation['product_image'] = instance.product_image.url
        if instance.upi_qr_image:
            representation['upi_qr_image'] = instance.upi_qr_image.url
        return representation

    def get_current_price(self, obj):
        """Get current dynamic unit price."""
        return obj.get_current_price()

    def get_time_remaining_seconds(self, obj):
        """Get time remaining in seconds for active countdowns."""
        rem = obj.time_remaining
        return int(rem.total_seconds()) if rem else 0

    def get_user_registration(self, obj):
        """Get user's registration details for the campaign if exists."""
        request = self.context.get('request')
        if not request or not request.user or not request.user.is_authenticated:
            return None
        registration = obj.registrations.filter(employee=request.user).exclude(payment_status__in=['cancelled', 'rejected']).first()
        if not registration:
            return None
        return {
            'id': registration.id,
            'payment_status': registration.payment_status,
            'is_waitlisted': registration.is_waitlisted,
            'waitlist_position': registration.waitlist_position,
        }

    def validate(self, attrs):
        """
        Validate Campaign attributes and Pricing Tiers constraints:
        1. End date calculation check.
        2. Pricing tiers must exist and be contiguous and non-overlapping.
        3. Token deposit and cancellation refund amount must be valid.
        """
        # Validate token deposit and cancellation refund amount
        token_deposit = attrs.get('token_deposit', Decimal('0.00'))
        cancellation_refund_amount = attrs.get('cancellation_refund_amount', Decimal('0.00'))

        if token_deposit < 0:
            raise serializers.ValidationError({"token_deposit": "Token deposit must be a non-negative number."})
        if cancellation_refund_amount < 0:
            raise serializers.ValidationError({"cancellation_refund_amount": "Cancellation refund amount must be a non-negative number."})
        if cancellation_refund_amount > token_deposit:
            raise serializers.ValidationError({"cancellation_refund_amount": "Cancellation refund amount cannot exceed the token deposit amount."})

        # Read the pricing tiers from initial data
        pricing_tiers_data = self.initial_data.get('pricing_tiers', [])
        if not pricing_tiers_data:
            raise serializers.ValidationError({"pricing_tiers": "At least one pricing tier is required."})

        # Validate structure and values of tiers
        try:
            sorted_tiers = sorted(pricing_tiers_data, key=lambda x: int(x['min_buyers']))
        except (ValueError, KeyError, TypeError):
            raise serializers.ValidationError({"pricing_tiers": "Pricing tiers must contain valid min_buyers integers."})

        for i in range(len(sorted_tiers)):
            min_buyers = int(sorted_tiers[i]['min_buyers'])
            max_buyers_val = sorted_tiers[i].get('max_buyers')
            max_buyers = int(max_buyers_val) if max_buyers_val is not None and max_buyers_val != '' else None
            price = float(sorted_tiers[i]['price'])

            if min_buyers < 1:
                raise serializers.ValidationError({"pricing_tiers": f"Min buyers must be at least 1 (found {min_buyers})."})
            
            if max_buyers is not None and max_buyers < min_buyers:
                raise serializers.ValidationError({"pricing_tiers": f"Max buyers ({max_buyers}) cannot be less than min buyers ({min_buyers})."})

            if price <= 0:
                raise serializers.ValidationError({"pricing_tiers": "Price must be a positive decimal."})

            if i < len(sorted_tiers) - 1:
                if max_buyers is None:
                    raise serializers.ValidationError({"pricing_tiers": "Only the final pricing tier can have an unlimited (null) max_buyers."})
                next_min = int(sorted_tiers[i+1]['min_buyers'])
                if next_min != max_buyers + 1:
                    raise serializers.ValidationError({"pricing_tiers": f"Pricing tiers must be contiguous. Milestone ending at {max_buyers} must be followed by tier starting at {max_buyers+1} (found {next_min})."})

        return attrs

    def create(self, validated_data):
        """Create Campaign and associated PricingTiers."""
        pricing_tiers_data = validated_data.pop('pricing_tiers', [])
        validated_data['available_quantity'] = validated_data['total_quantity']
        campaign = Campaign.objects.create(**validated_data)

        for tier in pricing_tiers_data:
            PricingTier.objects.create(campaign=campaign, **tier)

        return campaign

    def update(self, instance, validated_data):
        """Update Campaign and associate PricingTiers (only if no bookings exist)."""
        if instance.registrations.exclude(payment_status='cancelled').exists():
            raise serializers.ValidationError("Campaign details cannot be modified after registrations have been placed.")

        pricing_tiers_data = validated_data.pop('pricing_tiers', None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if 'total_quantity' in validated_data:
            instance.available_quantity = validated_data['total_quantity']

        instance.save()

        if pricing_tiers_data is not None:
            instance.pricing_tiers.all().delete()
            for tier in pricing_tiers_data:
                PricingTier.objects.create(campaign=instance, **tier)

        return instance


class CampaignRegistrationSerializer(serializers.ModelSerializer):
    """
    Serializer for CampaignRegistration.
    """
    employee_name = serializers.CharField(source='employee.name', read_only=True)
    employee_id = serializers.CharField(source='employee.employee_id', read_only=True)
    campaign_title = serializers.CharField(source='campaign.title', read_only=True)
    campaign_status = serializers.CharField(source='campaign.status', read_only=True)
    campaign_end_date = serializers.DateTimeField(source='campaign.end_date', read_only=True)
    campaign_token_deposit = serializers.DecimalField(source='campaign.token_deposit', read_only=True, max_digits=10, decimal_places=2)
    campaign_cancellation_refund_amount = serializers.DecimalField(source='campaign.cancellation_refund_amount', read_only=True, max_digits=10, decimal_places=2)

    class Meta:
        model = CampaignRegistration
        fields = [
            'id', 'campaign', 'campaign_title', 'campaign_status', 'campaign_end_date',
            'campaign_token_deposit', 'campaign_cancellation_refund_amount',
            'employee', 'employee_id', 'employee_name', 
            'reservation_date', 'token_amount', 'payment_status', 'upi_screenshot', 
            'cashfree_order_id', 'cashfree_payment_id', 'payment_approved_by', 
            'payment_approved_at', 'is_waitlisted', 'waitlist_position', 
            'cancellation_date', 'refund_amount', 'refund_status', 'slot_expiry_date'
        ]
        read_only_fields = [
            'id', 'employee', 'reservation_date', 'token_amount', 'payment_status', 
            'cashfree_order_id', 'cashfree_payment_id', 'payment_approved_by', 
            'payment_approved_at', 'is_waitlisted', 'waitlist_position', 
            'cancellation_date', 'refund_amount', 'refund_status', 'slot_expiry_date'
        ]

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if instance.upi_screenshot:
            representation['upi_screenshot'] = instance.upi_screenshot.url
        return representation


class TokenPaymentSerializer(serializers.ModelSerializer):
    """
    Serializer for TokenPayment.
    """
    campaign_title = serializers.CharField(source='registration.campaign.title', read_only=True)
    employee_name = serializers.CharField(source='registration.employee.name', read_only=True)
    employee_id = serializers.CharField(source='registration.employee.employee_id', read_only=True)

    class Meta:
        model = TokenPayment
        fields = [
            'id', 'registration', 'campaign_title', 'employee_name', 'employee_id', 
            'amount', 'cashfree_order_id', 'cashfree_payment_id', 'submitted_at', 
            'status', 'reviewed_by', 'reviewed_at', 'rejection_reason'
        ]
        read_only_fields = ['id', 'submitted_at', 'reviewed_by', 'reviewed_at']
