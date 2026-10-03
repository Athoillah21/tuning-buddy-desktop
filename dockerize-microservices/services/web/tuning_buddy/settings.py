"""
Django settings for the Tuning Buddy web service.
Runs locally in Docker; analysis, AI and PDF generation live in separate services.
"""

import os
from pathlib import Path
import dj_database_url
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


def env_list(name: str, default: str) -> list:
    return [value.strip() for value in os.environ.get(name, default).split(',') if value.strip()]


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-dev-key-change-in-production')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get('DEBUG', 'False').lower() == 'true'

ALLOWED_HOSTS = env_list('ALLOWED_HOSTS', 'localhost,127.0.0.1')

CSRF_TRUSTED_ORIGINS = env_list('CSRF_TRUSTED_ORIGINS', 'http://localhost:8000,http://127.0.0.1:8000')

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'advisor',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    # Desktop only (DESKTOP_ACCESS_TOKEN set): the app answers its own window and nothing else
    'advisor.desktop_access.DesktopAccessMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Must come after MessageMiddleware: locks the app until an AI provider is verified
    'advisor.middleware.AIReadyMiddleware',
]

SESSION_ENGINE = 'django.contrib.sessions.backends.signed_cookies'

ROOT_URLCONF = 'tuning_buddy.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'advisor.context_processors.ai_status',
            ],
        },
    },
]

WSGI_APPLICATION = 'tuning_buddy.wsgi.application'

# Database - the compose postgres container (SQLite fallback for running outside Docker)
DATABASE_URL = os.environ.get('DATABASE_URL')

if DATABASE_URL:
    DATABASES = {
        'default': dj_database_url.config(
            default=DATABASE_URL,
            conn_max_age=600,
            conn_health_checks=True,
        )
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    }
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
# The desktop launcher sets TIME_ZONE to the computer's own zone
TIME_ZONE = os.environ.get('TIME_ZONE', 'Asia/Jakarta')
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATICFILES_STORAGE = 'whitenoise.storage.CompressedStaticFilesStorage'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Custom settings for the app
ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY', '')

# 'docker' (compose stack) or 'desktop' (Windows app, all services on localhost); only changes UI hints
DEPLOYMENT_MODE = os.environ.get('DEPLOYMENT_MODE', 'docker')

# Backend services
AI_SERVICE_URL = os.environ.get('AI_SERVICE_URL', 'http://localhost:8002')
ANALYZER_URL = os.environ.get('ANALYZER_URL', 'http://localhost:8001')
REPORT_URL = os.environ.get('REPORT_URL', 'http://localhost:8003')

# Desktop hardening, both unset in the Docker stack. The launcher makes new values each run.
# INTERNAL_TOKEN goes with every call to the services (their security.py checks it);
# DESKTOP_ACCESS_TOKEN unlocks the web app for the app's own window (advisor/desktop_access.py).
INTERNAL_TOKEN = os.environ.get('TB_INTERNAL_TOKEN', '')
DESKTOP_ACCESS_TOKEN = os.environ.get('TB_DESKTOP_ACCESS_TOKEN', '')
ANALYZER_TIMEOUT = int(os.environ.get('ANALYZER_TIMEOUT', '900'))  # seconds; optimization can take minutes

# Timeout settings for external connections
DB_CONNECTION_TIMEOUT = int(os.environ.get('DB_CONNECTION_TIMEOUT', '5'))  # seconds
# Saved database passwords expire after this many hours; 0 keeps them until changed
PASSWORD_EXPIRY_HOURS = int(os.environ.get('PASSWORD_EXPIRY_HOURS', '1'))

# Logging Configuration
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'timestamped': {
            'format': '%(asctime)s %(levelname)s %(name)s: %(message)s',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'timestamped',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,  # the root logger has the same handler
        },
        'tuning_buddy': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,  # the root logger has the same handler
        },
    },
}
