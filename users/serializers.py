import re
from rest_framework import serializers
from .models import Employee, SavedProduct

class EmployeeSerializer(serializers.ModelSerializer):
    """
    Serializer for the Employee user model.
    Only allows updating mobile number and profile picture.
    All other fields (ID, Name, Email, Department, is_admin) are read-only.
    """
    class Meta:
        model = Employee
        fields = [
            'employee_id', 
            'name', 
            'email', 
            'mobile', 
            'department', 
            'is_active', 
            'is_admin', 
            'date_joined', 
            'profile_picture'
        ]
        read_only_fields = [
            'employee_id', 
            'name', 
            'email', 
            'department', 
            'is_active', 
            'is_admin', 
            'date_joined'
        ]

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if instance.profile_picture:
            representation['profile_picture'] = instance.profile_picture.url
        return representation

    def validate_mobile(self, value):
        """
        Validate that mobile number is a valid 10-digit number.
        Allow null/blank values as it is optional.
        """
        if value:
            # Strip any whitespace
            value = value.strip()
            # Match standard Indian 10-digit mobile number format
            if not re.match(r'^[6-9]\d{9}$', value):
                raise serializers.ValidationError("Mobile number must be a valid 10-digit Indian number starting with 6, 7, 8 or 9.")
        return value

    def validate_profile_picture(self, value):
        """
        Validate profile picture format and size (max 5MB).
        """
        if value:
            # Validate size (5MB = 5 * 1024 * 1024 bytes)
            if hasattr(value, 'size') and value.size > 5 * 1024 * 1024:
                raise serializers.ValidationError("Profile picture size cannot exceed 5MB.")
            
            # Validate file extension/type
            name = getattr(value, 'name', '').lower()
            if name and not name.endswith(('.jpg', '.jpeg', '.png', '.webp')):
                raise serializers.ValidationError("Only JPG, JPEG, PNG, and WEBP image formats are allowed.")
        return value


class OTPRequestSerializer(serializers.Serializer):
    """
    Validates payload for requesting a login OTP.
    Requires employee_id and email address.
    """
    employee_id = serializers.CharField(max_length=20, required=True)
    email = serializers.EmailField(required=True)

    def validate_employee_id(self, value):
        """
        Validate employee_id format (must be alphanumeric).
        """
        value = value.strip()
        if not value.isalnum():
            raise serializers.ValidationError("Employee ID must be alphanumeric.")
        return value

    def validate_email(self, value):
        """
        Validate and normalize email address.
        """
        return value.strip().lower()


class OTPVerifySerializer(serializers.Serializer):
    """
    Validates payload for verifying OTP code and completing login.
    Requires employee_id and 6-digit OTP code.
    """
    employee_id = serializers.CharField(max_length=20, required=True)
    otp_code = serializers.CharField(max_length=6, min_length=6, required=True)

    def validate_employee_id(self, value):
        """Ensure employee ID is clean."""
        return value.strip()

    def validate_otp_code(self, value):
        """
        Validate that the OTP code contains exactly 6 digits.
        """
        value = value.strip()
        if not value.isdigit():
            raise serializers.ValidationError("OTP code must contain digits only.")
        return value


class SavedProductSerializer(serializers.ModelSerializer):
    """
    Serializer for the SavedProduct model.
    Enables employees to wishlist/save marketplace listings.
    """
    class Meta:
        model = SavedProduct
        fields = ['id', 'employee', 'marketplace_listing', 'saved_at']
        read_only_fields = ['id', 'employee', 'saved_at']

    def to_representation(self, instance):
        """
        Dynamically import and use MarketplaceListingSerializer to avoid circular imports.
        """
        from marketplace.serializers import MarketplaceListingSerializer
        representation = super().to_representation(instance)
        representation['marketplace_listing'] = MarketplaceListingSerializer(
            instance.marketplace_listing,
            context=self.context
        ).data
        return representation

