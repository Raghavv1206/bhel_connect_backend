import os
from django.db import models
from django.conf import settings
from cloudinary.models import CloudinaryField as BaseCloudinaryField
from django.core.files.uploadedfile import UploadedFile

def is_cloudinary_configured():
    config = getattr(settings, 'CLOUDINARY_STORAGE', {})
    cloud_name = config.get('CLOUD_NAME', '')
    api_key = config.get('API_KEY', '')
    api_secret = config.get('API_SECRET', '')
    return bool(
        cloud_name and not str(cloud_name).startswith('your_') and
        api_key and not str(api_key).startswith('your_') and
        api_secret and not str(api_secret).startswith('your_')
    )

def is_local_path(value):
    """Checks if the stored path is a local file path rather than a Cloudinary ID."""
    if not isinstance(value, str):
        return False
    # All our local uploads start with 'uploads/'
    # Seeded or other media might start with 'profiles/', 'products/'
    return value.startswith(('uploads/', 'profiles/', 'products/'))

class LocalMediaResource:
    """Mock CloudinaryResource that points to local media path in dev environments."""
    def __init__(self, path):
        self.path = path

    @property
    def url(self):
        # If the path is already a full URL (e.g. from seeded database), return it as is
        if str(self.path).startswith(('http://', 'https://')):
            return self.path
        media_url = getattr(settings, 'MEDIA_URL', '/media/')
        # Prepend the absolute BACKEND_URL so frontend can resolve local images directly
        backend_url = os.environ.get('BACKEND_URL', '').rstrip('/')
        if not backend_url:
            backend_url = 'http://localhost:8000'
        return f"{backend_url}{media_url}{self.path}"

    def __str__(self):
        return str(self.path)

class CloudinaryField(BaseCloudinaryField):
    """
    Subclasses CloudinaryField to support transparent local fallback when
    Cloudinary API credentials are placeholders or not configured.
    """
    def pre_save(self, model_instance, add):
        if not is_cloudinary_configured():
            file = getattr(model_instance, self.attname)
            # If a new file is uploaded
            if file and hasattr(file, 'file'):
                from django.core.files.storage import default_storage
                import uuid
                ext = os.path.splitext(file.name)[1]
                filename = default_storage.save(f"uploads/{uuid.uuid4()}{ext}", file)
                setattr(model_instance, self.attname, filename)
                return filename
            # If value is LocalMediaResource, return its raw string path
            if isinstance(file, LocalMediaResource):
                return file.path
            return str(file) if file else None
        return super().pre_save(model_instance, add)

    def to_python(self, value):
        if value is None or value == "":
            return None
        if isinstance(value, UploadedFile):
            return value
        if isinstance(value, LocalMediaResource):
            return value
        if not is_cloudinary_configured() or is_local_path(value):
            return LocalMediaResource(value)
        return super().to_python(value)

    def from_db_value(self, value, expression, connection, *args, **kwargs):
        if value is None or value == "":
            return None
        if not is_cloudinary_configured() or is_local_path(value):
            return LocalMediaResource(value)
        return super().from_db_value(value, expression, connection, *args, **kwargs)

    def get_prep_value(self, value):
        if not is_cloudinary_configured() or isinstance(value, LocalMediaResource):
            if not value:
                return self.get_default()
            if isinstance(value, LocalMediaResource):
                return value.path
            if isinstance(value, UploadedFile):
                return str(value)
            return str(value)
        return super().get_prep_value(value)
