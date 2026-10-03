"""
Database models for the Query Tuning Advisor.
Stores database connections, query history, and recommendations.
"""
from django.db import models
from django.conf import settings
from cryptography.fernet import Fernet, InvalidToken
from django.core.exceptions import ImproperlyConfigured


# Hosts on this computer (or, for the Docker stack, its own network): no network to listen on
LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1', 'host.docker.internal', 'sampledb'}
# libpq modes that silently fall back to an unencrypted connection (or never encrypt)
WEAK_SSL_MODES = {'disable', 'allow', 'prefer'}


def is_local_host(host: str) -> bool:
    return (host or '').strip().lower().strip('[]') in LOCAL_HOSTS


class EncryptedFieldMixin:
    """
    Encrypting/decrypting field values with ENCRYPTION_KEY. Fails closed: without a usable key
    nothing is stored at all, rather than stored in plain text.
    """

    @staticmethod
    def _fernet() -> Fernet:
        if not settings.ENCRYPTION_KEY:
            raise ImproperlyConfigured("ENCRYPTION_KEY is not set: connection secrets would be stored unencrypted.")
        try:
            return Fernet(settings.ENCRYPTION_KEY.encode())
        except (ValueError, TypeError) as e:
            raise ImproperlyConfigured(f"ENCRYPTION_KEY is not a valid Fernet key: {e}") from e

    @classmethod
    def encrypt(cls, value: str) -> str:
        """Encrypt a string value."""
        if not value:
            return value
        return cls._fernet().encrypt(value.encode()).decode()

    @classmethod
    def decrypt(cls, value: str) -> str:
        """Decrypt a stored value; one that is not encrypted (from long ago) is returned as it is."""
        if not value:
            return value
        try:
            return cls._fernet().decrypt(value.encode()).decode()
        except InvalidToken:
            return value

    @classmethod
    def is_encrypted(cls, value: str) -> bool:
        """Decided by decrypting, not by its look: a password may well start like a token."""
        if not value:
            return False
        try:
            cls._fernet().decrypt(value.encode())
            return True
        except InvalidToken:
            return False


class Connection(models.Model, EncryptedFieldMixin):
    """
    Stores PostgreSQL database connection details.
    Sensitive fields (host, username, password) are encrypted.
    """
    SSL_MODES = [
        ('disable', 'Disable'),
        ('allow', 'Allow'),
        ('prefer', 'Prefer'),
        ('require', 'Require'),
        ('verify-ca', 'Verify CA'),
        ('verify-full', 'Verify Full'),
    ]
    
    name = models.CharField(max_length=100, help_text="Friendly name for this connection")
    host = models.TextField(help_text="Database host (encrypted)")
    port = models.IntegerField(default=5432)
    database = models.CharField(max_length=100)
    username = models.TextField(help_text="Database username (encrypted)")
    password = models.TextField(help_text="Database password (encrypted)")
    ssl_mode = models.CharField(max_length=20, choices=SSL_MODES, default='prefer')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    password_updated_at = models.DateTimeField(auto_now_add=True, help_text="When password was last set")
    
    class Meta:
        ordering = ['-updated_at']
    
    def __str__(self):
        return f"{self.name} ({self.database})"
    
    def save(self, *args, **kwargs):
        """Encrypt sensitive fields before saving."""
        # Track if password changed (for expiry tracking)
        password_changed = False
        if self.pk:
            try:
                old = Connection.objects.get(pk=self.pk)
                if self.password != old.password:
                    password_changed = True
            except Connection.DoesNotExist:
                password_changed = True
        else:
            password_changed = True
        
        # Only encrypt what is not encrypted yet
        if self.host and not self.is_encrypted(self.host):
            self.host = self.encrypt(self.host)
        if self.username and not self.is_encrypted(self.username):
            self.username = self.encrypt(self.username)
        if self.password and not self.is_encrypted(self.password):
            self.password = self.encrypt(self.password)
            password_changed = True
        
        # Update password timestamp if password changed
        if password_changed:
            from django.utils import timezone
            self.password_updated_at = timezone.now()
        
        super().save(*args, **kwargs)
    
    def is_password_expired(self) -> bool:
        """
        Whether the saved password is older than PASSWORD_EXPIRY_HOURS.
        Set that to 0 to keep credentials until they are changed.
        """
        from django.utils import timezone
        from datetime import timedelta
        hours = getattr(settings, 'PASSWORD_EXPIRY_HOURS', 1)
        if not hours:
            return False
        if not self.password_updated_at:
            return True
        return timezone.now() - self.password_updated_at > timedelta(hours=hours)
    
    def get_decrypted_host(self) -> str:
        return self.decrypt(self.host)

    @property
    def may_be_unencrypted(self) -> bool:
        """A server on another computer, with an SSL mode that can fall back to plain text."""
        return self.ssl_mode in WEAK_SSL_MODES and not is_local_host(self.get_decrypted_host())
    
    def get_decrypted_username(self) -> str:
        return self.decrypt(self.username)
    
    def get_decrypted_password(self) -> str:
        return self.decrypt(self.password)
    
    def get_connection_params(self) -> dict:
        """Return decrypted connection parameters for psycopg2."""
        return {
            'host': self.get_decrypted_host(),
            'port': self.port,
            'database': self.database,
            'user': self.get_decrypted_username(),
            'password': self.get_decrypted_password(),
            'sslmode': self.ssl_mode,
            'connect_timeout': settings.DB_CONNECTION_TIMEOUT,
        }


class QueryHistory(models.Model):
    """
    Stores the history of analyzed queries.
    """
    # PROTECT, not CASCADE: deleting a connection must never silently erase saved analyses
    connection = models.ForeignKey(Connection, on_delete=models.PROTECT, related_name='queries')
    original_query = models.TextField(help_text="The original SQL query")
    original_plan = models.JSONField(null=True, blank=True, help_text="EXPLAIN ANALYZE output as JSON")
    original_execution_time = models.FloatField(null=True, blank=True, help_text="Median execution time in milliseconds")
    original_execution_times = models.JSONField(default=list, null=True, blank=True, help_text="Every repeat measurement of the baseline")
    ai_provider = models.JSONField(null=True, blank=True, help_text="AI provider used for recommendations")
    analysis_status = models.CharField(
        max_length=20,
        choices=[
            ('pending', 'Pending'),
            ('analyzing', 'Analyzing'),
            ('completed', 'Completed'),
            ('failed', 'Failed'),
        ],
        default='pending'
    )
    error_message = models.TextField(null=True, blank=True)
    # Size, rows, indexes and partitioning of every table in the query, as they were when
    # it was analyzed (keyed by the name the query uses). Empty for analyses before 1.1.
    table_stats = models.JSONField(default=dict, null=True, blank=True, help_text="Catalog facts about each table in the query")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'Query histories'
    
    def __str__(self):
        return f"Query #{self.id} - {self.original_query[:50]}..."


class Recommendation(models.Model):
    """
    Stores optimization recommendations from Gemini.
    """
    RECOMMENDATION_TYPES = [
        ('index', 'Add Index'),
        ('rewrite', 'Query Rewrite'),
        ('config', 'Configuration Change'),
        ('schema', 'Schema Change'),
    ]
    
    query_history = models.ForeignKey(QueryHistory, on_delete=models.CASCADE, related_name='recommendations')
    recommendation_type = models.CharField(max_length=20, choices=RECOMMENDATION_TYPES)
    description = models.TextField(help_text="Explanation of the optimization")
    optimized_query = models.TextField(null=True, blank=True, help_text="Rewritten query if applicable")
    suggested_indexes = models.JSONField(default=list, help_text="List of CREATE INDEX statements")
    tested_execution_time = models.FloatField(null=True, blank=True, help_text="Tested execution time in ms")
    tested_plan = models.JSONField(null=True, blank=True, help_text="Execution plan from testing")
    improvement_percentage = models.FloatField(null=True, blank=True)
    rank = models.IntegerField(default=0, help_text="Ranking by improvement")
    gemini_raw_response = models.JSONField(null=True, blank=True, help_text="Raw Gemini response for debugging")
    
    # Accumulated data from iterative optimization
    all_indexes_applied = models.JSONField(default=list, null=True, blank=True, help_text="All indexes created across iterations")
    final_optimized_query = models.TextField(null=True, blank=True, help_text="Final query after all iterations")
    query_was_rewritten = models.BooleanField(default=False, help_text="Whether query was modified from original")
    optimization_attempts = models.IntegerField(default=1, help_text="Number of optimization iterations")
    seq_scan_eliminated = models.BooleanField(default=False, help_text="Whether seq scan was eliminated")

    # Measurement honesty: a percentage means nothing without the noise around it
    VERDICTS = [
        ('faster', 'Faster'),
        ('slower', 'Slower'),
        ('within_noise', 'Within measurement noise'),
        ('already_fast', 'Already below the noise floor'),
        ('unknown', 'Not measured'),
    ]
    verdict = models.CharField(max_length=20, choices=VERDICTS, blank=True, default='', help_text="Whether the difference is significant")
    measurement_spread_ms = models.FloatField(null=True, blank=True, help_text="Spread between repeated measurements")
    tested_execution_times = models.JSONField(default=list, null=True, blank=True, help_text="Every repeat measurement of the tested query")

    # Correctness: a rewrite must return what the original returned
    RESULT_CHECKS = [
        ('same', 'Same results'),
        ('different', 'Different results'),
        ('unchecked', 'Not compared'),
    ]
    result_check = models.CharField(max_length=20, choices=RESULT_CHECKS, blank=True, default='', help_text="Whether the rewritten query returns the original's rows")
    result_check_note = models.TextField(blank=True, default='', help_text="What the result comparison found")

    # Fit: does the recommendation suit the database as it is?
    fit_checks = models.JSONField(default=list, null=True, blank=True, help_text="Findings about existing indexes, table size and partitioning")
    index_sizes = models.JSONField(default=list, null=True, blank=True, help_text="Measured size of each suggested index")

    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['rank']
    
    def __str__(self):
        return f"Recommendation #{self.id} - {self.recommendation_type}"
    
