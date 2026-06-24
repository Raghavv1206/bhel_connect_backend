import os
from pathlib import Path
from datetime import timedelta
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env file
load_dotenv(os.path.join(BASE_DIR, '.env'))

# SECRET KEY - read from env or fallback to a dev key (never expose in prod)
SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-l8!wc!(%29-_8you_3ftl6=nb&!d8!w#kwb+0050ley9l%t2jb')

# DEBUG status - default to False for safety
DEBUG = os.environ.get('DEBUG', 'False').lower() in ('true', '1')

# ALLOWED HOSTS
ALLOWED_HOSTS = [host.strip() for host in os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')]

# CSRF Trusted Origins for Django 4.0+ (Automatic based on ALLOWED_HOSTS, with environment override)
csrf_env = os.environ.get('CSRF_TRUSTED_ORIGINS')
if csrf_env:
    CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in csrf_env.split(',')]
else:
    CSRF_TRUSTED_ORIGINS = [
        f"https://{host}" for host in ALLOWED_HOSTS if host != '*' and not (host.startswith('localhost') or host.startswith('127.0.0.1'))
    ] + [
        f"http://{host}" for host in ALLOWED_HOSTS if host != '*' and (host.startswith('localhost') or host.startswith('127.0.0.1'))
    ]



# Application definition
INSTALLED_APPS = [
    'daphne',  # Daphne must be listed before staticfiles for ASGI
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # Third Party Apps
    'rest_framework',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    'django_filters',
    'cloudinary',
    'cloudinary_storage',
    'channels',
    
    # Internal Project Apps
    'users',
    'smartbuy',
    'marketplace',
    'adminpanel',
    'reports',
    'notifications',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  # Replaces Nginx for serving static files in production
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',  # CorsMiddleware must be placed before CommonMiddleware
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Audit log — must come AFTER AuthenticationMiddleware so request.user is resolved
    'adminpanel.middleware.AuditLogMiddleware',
]

ROOT_URLCONF = 'bhel_connect_backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'bhel_connect_backend.wsgi.application'
ASGI_APPLICATION = 'bhel_connect_backend.asgi.application'


import sys
import urllib.parse as urlparse

# Database Configuration
# Primary: PostgreSQL (with SQLite fallback for local development)
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('DB_NAME', 'bhel_connect'),
        'USER': os.environ.get('DB_USER', 'bhel_user'),
        'PASSWORD': os.environ.get('DB_PASSWORD', 'your_db_password'),
        'HOST': os.environ.get('DB_HOST', 'localhost'),
        'PORT': os.environ.get('DB_PORT', '5432'),
        'CONN_MAX_AGE': 0 if 'test' in sys.argv else 600,
    }
}

# Support DATABASE_URL from Render or other hosting providers
db_url = os.environ.get('DATABASE_URL')
if db_url:
    url = urlparse.urlparse(db_url)
    DATABASES['default'] = {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': urlparse.unquote(url.path[1:]) if url.path else '',
        'USER': urlparse.unquote(url.username) if url.username else '',
        'PASSWORD': urlparse.unquote(url.password) if url.password else '',
        'HOST': url.hostname,
        'PORT': url.port or '5432',
        'CONN_MAX_AGE': 0 if 'test' in sys.argv else 600,
    }

# Fallback database if we are running in debug mode and postgres is not configured or uses default placeholders
db_pass = os.environ.get('DB_PASSWORD', '')
if 'test' in sys.argv:
    DATABASES['default'] = {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': str(BASE_DIR / 'db_test.sqlite3'),
    }
elif not db_url and DEBUG and (not db_pass or db_pass == 'your_db_password' or db_pass == 'your_db_password_here'):
    DATABASES['default'] = {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': str(BASE_DIR / 'db.sqlite3'),
    }


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True


# Static & Media Storage Configuration (Cloudinary integration)
STATIC_URL = 'static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

CLOUDINARY_STORAGE = {
    'CLOUD_NAME': os.environ.get('CLOUDINARY_CLOUD_NAME'),
    'API_KEY': os.environ.get('CLOUDINARY_API_KEY'),
    'API_SECRET': os.environ.get('CLOUDINARY_API_SECRET'),
}

# Auto-fallback to local filesystem storage if Cloudinary is not configured or uses placeholders
cloud_name = CLOUDINARY_STORAGE.get('CLOUD_NAME', '')
api_key = CLOUDINARY_STORAGE.get('API_KEY', '')
api_secret = CLOUDINARY_STORAGE.get('API_SECRET', '')

is_cloudinary_configured = bool(
    cloud_name and not cloud_name.startswith('your_') and
    api_key and not api_key.startswith('your_') and
    api_secret and not api_secret.startswith('your_')
)

# Modern STORAGES configuration (Django 4.2+) — replaces deprecated DEFAULT_FILE_STORAGE and STATICFILES_STORAGE
STORAGES = {
    'default': {
        'BACKEND': 'cloudinary_storage.storage.MediaCloudinaryStorage' if is_cloudinary_configured else 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}


MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# Limit max request body size to 10MB (prevents abuse via large uploads)
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB


# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Custom User Model
AUTH_USER_MODEL = 'users.Employee'


# Django REST Framework Settings
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
    ],
}


# SimpleJWT JWT Configuration
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=1),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'USER_ID_FIELD': 'employee_id',
    'USER_ID_CLAIM': 'employee_id',
}


# CORS Configuration
CORS_ALLOWED_ORIGINS = [
    origin.strip() for origin in os.environ.get('CORS_ALLOWED_ORIGINS', 'http://localhost:5173,http://127.0.0.1:5173').split(',')
]
CORS_ALLOW_CREDENTIALS = True


# Email API Configuration (HTTP-based Transactional Email)
# Integrates with django-anymail to support SendGrid, Brevo, Resend, or SMTP/Console fallbacks.
EMAIL_BACKEND_PROVIDER = os.environ.get('EMAIL_BACKEND_PROVIDER', 'smtp').lower()

if DEBUG:
    # Use console email backend for local development convenience to avoid needing API keys
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
elif EMAIL_BACKEND_PROVIDER == 'sendgrid':
    EMAIL_BACKEND = 'anymail.backends.sendgrid.EmailBackend'
    ANYMAIL = {
        'SENDGRID_API_KEY': os.environ.get('SENDGRID_API_KEY'),
    }
elif EMAIL_BACKEND_PROVIDER == 'brevo':
    EMAIL_BACKEND = 'anymail.backends.brevo.EmailBackend'
    ANYMAIL = {
        'BREVO_API_KEY': os.environ.get('BREVO_API_KEY'),
    }
elif EMAIL_BACKEND_PROVIDER == 'resend':
    EMAIL_BACKEND = 'anymail.backends.resend.EmailBackend'
    ANYMAIL = {
        'RESEND_API_KEY': os.environ.get('RESEND_API_KEY'),
    }
else:
    # Fallback to SMTP if EMAIL_BACKEND_PROVIDER is set to 'smtp' or not specified
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'

# Standard Django email settings used by the SMTP backend or as defaults
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', 587))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD')
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'True').lower() in ('true', '1')
EMAIL_TIMEOUT = 10  # Enforce 10-second socket timeout to prevent thread leaks when ports are blocked

# Default sender identity displayed to users
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'BHEL Connect <getmeachai.noreply@gmail.com>')


# Redis Channel Layers (Required for WebSockets / Django Channels)
redis_url = os.environ.get('REDIS_URL')
use_redis = False

if redis_url and not DEBUG:
    # In production, always require Redis
    use_redis = True
elif redis_url and DEBUG:
    # In local development, check if Redis is active; if not, fall back to InMemory
    try:
        import redis
        r = redis.Redis.from_url(redis_url, socket_timeout=1)
        r.ping()
        use_redis = True
    except Exception:
        use_redis = False

if use_redis:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {
                'hosts': [redis_url],
            },
        },
    }
else:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels.layers.InMemoryChannelLayer',
        },
    }


# Cache Configuration (Ensures production rate limits are synchronized using Redis)
if use_redis:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': redis_url,
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'bhel_connect_cache',
        }
    }


# Security Headers for Production Compliance
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

# HTTPS enforcement & HSTS headers (production only)
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')  # Required for Render/Railway/Heroku
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
