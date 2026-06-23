from rest_framework import serializers
from .models import Category, MarketplaceListing, ListingImage, VehicleListing, PropertyListing

class CategorySerializer(serializers.ModelSerializer):
    """
    Serializer for the hierarchical Category model.
    """
    class Meta:
        model = Category
        fields = ['id', 'name', 'slug', 'parent']


class ListingImageSerializer(serializers.ModelSerializer):
    """
    Serializer for Marketplace Listing images.
    """
    class Meta:
        model = ListingImage
        fields = ['id', 'image', 'is_primary', 'uploaded_at']

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if instance.image:
            representation['image'] = instance.image.url
        return representation


class VehicleListingSerializer(serializers.ModelSerializer):
    """
    Serializer for vehicle specific detail fields.
    """
    class Meta:
        model = VehicleListing
        fields = ['brand', 'model', 'year', 'km_driven', 'fuel_type', 'transmission']


class PropertyListingSerializer(serializers.ModelSerializer):
    """
    Serializer for property specific detail fields.
    """
    class Meta:
        model = PropertyListing
        fields = ['location', 'area_sqft', 'bedrooms', 'bathrooms', 'property_type', 'listing_type']


class MarketplaceListingSerializer(serializers.ModelSerializer):
    """
    Serializer for Employee Marketplace listings.
    Supports nested Category, ListingImage, VehicleListing, and PropertyListing serializers.
    """
    category = CategorySerializer(read_only=True)
    category_id = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(),
        source='category',
        write_only=True,
        required=True
    )
    images = ListingImageSerializer(many=True, read_only=True)
    vehicle_details = VehicleListingSerializer(required=False, allow_null=True)
    property_details = PropertyListingSerializer(required=False, allow_null=True)
    seller_name = serializers.CharField(source='seller.name', read_only=True)
    seller_department = serializers.CharField(source='seller.department', read_only=True)
    is_saved = serializers.SerializerMethodField()

    class Meta:
        model = MarketplaceListing
        fields = [
            'id', 'seller', 'seller_name', 'seller_department', 'title', 
            'description', 'price', 'condition', 'category', 'category_id', 'status', 
            'rejection_reason', 'views', 'created_at', 'updated_at', 'images',
            'vehicle_details', 'property_details', 'is_saved'
        ]
        read_only_fields = ['id', 'seller', 'status', 'rejection_reason', 'views', 'created_at', 'updated_at']

    def get_is_saved(self, obj):
        request = self.context.get('request')
        if request and request.user and request.user.is_authenticated:
            from users.models import SavedProduct
            return SavedProduct.objects.filter(employee=request.user, marketplace_listing=obj).exists()
        return False

    def to_internal_value(self, data):
        """
        Dynamically map flat multipart form-data keys to the nested objects expected by
        VehicleListingSerializer and PropertyListingSerializer.
        """
        if hasattr(data, 'dict'):
            internal_data = data.dict()
        else:
            internal_data = data.copy() if hasattr(data, 'copy') else dict(data)
        
        # Parse flat vehicle listing fields into vehicle_details object
        if any(k in internal_data for k in ['vehicle_brand', 'vehicle_model', 'vehicle_year', 'vehicle_km_driven', 'vehicle_fuel_type', 'vehicle_transmission']):
            # Convert empty strings to None
            brand = internal_data.get('vehicle_brand') or None
            model = internal_data.get('vehicle_model') or None
            year = internal_data.get('vehicle_year') or None
            km_driven = internal_data.get('vehicle_km_driven') or None
            fuel_type = internal_data.get('vehicle_fuel_type') or None
            transmission = internal_data.get('vehicle_transmission') or None
            
            if any([brand, model, year, km_driven, fuel_type, transmission]):
                internal_data['vehicle_details'] = {
                    'brand': brand,
                    'model': model,
                    'year': int(year) if year else None,
                    'km_driven': int(km_driven) if km_driven else None,
                    'fuel_type': fuel_type,
                    'transmission': transmission,
                }

        # Parse flat property listing fields into property_details object
        if any(k in internal_data for k in ['property_location', 'property_area_sqft', 'property_bedrooms', 'property_bathrooms', 'property_property_type', 'property_listing_type']):
            location = internal_data.get('property_location') or None
            area_sqft = internal_data.get('property_area_sqft') or None
            bedrooms = internal_data.get('property_bedrooms') or None
            bathrooms = internal_data.get('property_bathrooms') or None
            property_type = internal_data.get('property_property_type') or None
            listing_type = internal_data.get('property_listing_type') or None
            
            if any([location, area_sqft, bedrooms, bathrooms, property_type, listing_type]):
                internal_data['property_details'] = {
                    'location': location,
                    'area_sqft': float(area_sqft) if area_sqft else None,
                    'bedrooms': int(bedrooms) if bedrooms else 0,
                    'bathrooms': int(bathrooms) if bathrooms else 0,
                    'property_type': property_type,
                    'listing_type': listing_type,
                }
                
        return super().to_internal_value(internal_data)

    def validate(self, attrs):
        """
        Custom validation:
        1. Handle files in context (up to 5 images).
        """
        request = self.context.get('request')
        if request and request.method in ['POST', 'PUT', 'PATCH']:
            # Check image files upload limit
            files = request.FILES.getlist('images')
            if files:
                if len(files) > 5:
                    raise serializers.ValidationError({"images": "You can upload a maximum of 5 images per listing."})
                for file in files:
                    if file.size > 5 * 1024 * 1024:
                        raise serializers.ValidationError({"images": f"Image '{file.name}' exceeds the 5MB size limit."})
                    ext = file.name.split('.')[-1].lower() if '.' in file.name else ''
                    if ext not in ['jpg', 'jpeg', 'png', 'webp']:
                        raise serializers.ValidationError({"images": f"Image '{file.name}' must be in JPG, JPEG, PNG, or WEBP format."})
        return attrs

    def create(self, validated_data):
        """
        Custom create to handle vehicle_details and property_details nested writes and images.
        """
        vehicle_data = validated_data.pop('vehicle_details', None)
        property_data = validated_data.pop('property_details', None)
        
        listing = MarketplaceListing.objects.create(**validated_data)
        
        if vehicle_data:
            VehicleListing.objects.create(listing=listing, **vehicle_data)
        elif property_data:
            PropertyListing.objects.create(listing=listing, **property_data)
            
        # Handle images upload
        request = self.context.get('request')
        if request:
            images = request.FILES.getlist('images')
            for i, img in enumerate(images[:5]):
                is_primary = (i == 0)
                ListingImage.objects.create(listing=listing, image=img, is_primary=is_primary)

        return listing

    def update(self, instance, validated_data):
        """
        Custom update to handle nested writes for vehicle/property details and images.
        """
        vehicle_data = validated_data.pop('vehicle_details', None)
        property_data = validated_data.pop('property_details', None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if vehicle_data:
            VehicleListing.objects.update_or_create(listing=instance, defaults=vehicle_data)
        if property_data:
            PropertyListing.objects.update_or_create(listing=instance, defaults=property_data)

        # Handle images update (replace existing images if new ones are uploaded)
        request = self.context.get('request')
        if request:
            images = request.FILES.getlist('images')
            if images:
                # Delete old images
                instance.images.all().delete()
                # Create new images
                for i, img in enumerate(images[:5]):
                    is_primary = (i == 0)
                    ListingImage.objects.create(listing=instance, image=img, is_primary=is_primary)

        return instance
