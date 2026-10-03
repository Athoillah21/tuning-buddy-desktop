"""
Forms for the Query Tuning Advisor.
"""
import re

from django import forms
from .models import WEAK_SSL_MODES, Connection, is_local_host

# Whitespace, -- line comments, /* block comments */ and opening brackets before a query's first word
LEADING_NOISE_RE = re.compile(r'^(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/|\()+', re.S)

AI_PROVIDER_TYPE_CHOICES = [
    ('gemini', 'Google Gemini'),
    ('openai_compatible', 'OpenAI-compatible (OpenAI, DeepSeek, Groq, OpenRouter, Ollama, ...)'),
    ('anthropic', 'Anthropic Claude'),
]
AI_PROVIDER_TYPES_REQUIRING_KEY = {'gemini', 'anthropic'}


class ConnectionForm(forms.ModelForm):
    """Form for creating/editing database connections."""
    
    # Override password to use PasswordInput
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-input',
            'placeholder': 'Database password',
            'autocomplete': 'new-password',
        }),
        required=True,
    )
    
    class Meta:
        model = Connection
        fields = ['name', 'host', 'port', 'database', 'username', 'password', 'ssl_mode']
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'My Production DB',
            }),
            'host': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'db.example.com or IP address',
            }),
            'port': forms.NumberInput(attrs={
                'class': 'form-input',
                'placeholder': '5432',
            }),
            'database': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'database_name',
            }),
            'username': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'postgres',
            }),
            'ssl_mode': forms.Select(attrs={
                'class': 'form-select',
            }),
        }

    allow_unencrypted = forms.BooleanField(
        required=False,
        label='Allow an unencrypted connection (only on a network you trust)',
    )

    def clean(self):
        """
        A server on another computer gets an encrypted connection unless the user says otherwise:
        Disable, Allow and Prefer can all end up sending the password exchange, queries and results
        in plain text across the network.
        """
        cleaned = super().clean()
        host, ssl_mode = cleaned.get('host'), cleaned.get('ssl_mode')
        if (host and ssl_mode in WEAK_SSL_MODES and not is_local_host(host)
                and not cleaned.get('allow_unencrypted')):
            self.add_error('ssl_mode', 'This server is not on this computer. Choose "Require" (or "Verify Full" '
                                       'to also check its certificate), or tick "Allow an unencrypted connection".')
        return cleaned


class QueryForm(forms.Form):
    """Form for submitting queries for analysis."""
    
    query = forms.CharField(
        widget=forms.Textarea(attrs={
            'class': 'form-textarea code-editor',
            'placeholder': 'Enter your SQL query here...\n\nExample:\nSELECT * FROM users WHERE email = \'test@example.com\';',
            'rows': 10,
            'spellcheck': 'false',
        }),
        required=True,
        help_text='Enter a SELECT query to analyze. Only SELECT and WITH (CTE) queries are supported.',
    )
    
    test_recommendations = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={
            'class': 'form-checkbox',
        }),
        help_text='Test recommendations using temporary tables (may take longer)',
    )
    confirm_remote = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-checkbox'}),
    )
    
    def clean_query(self):
        query = self.cleaned_data['query'].strip()
        if not query:
            raise forms.ValidationError('Query cannot be empty')
        # Analyze runs the query (EXPLAIN ANALYZE). The analyzer enforces this exactly; this is the
        # early, friendly version of the same rule.
        first_word = LEADING_NOISE_RE.sub('', query).split(None, 1)
        if not first_word or first_word[0].upper().rstrip(';') not in ('SELECT', 'WITH', 'VALUES', 'TABLE'):
            raise forms.ValidationError('Analyze runs your query to measure it, so it only takes a read-only '
                                        'query: start with SELECT or WITH. To change data, use the Query tool '
                                        'with "Allow changes".')
        return query


class AIProviderForm(forms.Form):
    """Form for adding/editing an AI provider (stored by the AI service)."""

    name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'e.g. Claude primary',
        }),
    )

    provider_type = forms.ChoiceField(
        choices=AI_PROVIDER_TYPE_CHOICES,
        widget=forms.Select(attrs={
            'class': 'form-select',
        }),
    )

    base_url = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'https://api.example.com/v1',
        }),
    )

    api_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(attrs={
            'class': 'form-input',
            'placeholder': 'API key',
            'autocomplete': 'new-password',
        }),
    )

    clear_api_key = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={
            'class': 'form-checkbox',
        }),
    )

    model = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'Model name',
        }),
    )

    priority = forms.IntegerField(
        min_value=0,
        initial=100,
        widget=forms.NumberInput(attrs={
            'class': 'form-input',
        }),
        help_text='Lower numbers are tried first.',
    )

    enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={
            'class': 'form-checkbox',
        }),
    )

    def __init__(self, *args, has_api_key=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_api_key = has_api_key
        if has_api_key:
            self.fields['api_key'].widget.attrs['placeholder'] = 'Leave blank to keep the saved key'

    def clean(self):
        cleaned_data = super().clean()
        provider_type = cleaned_data.get('provider_type')

        if provider_type == 'openai_compatible' and not cleaned_data.get('base_url'):
            self.add_error('base_url', 'A base URL is required for OpenAI-compatible providers.')

        keeps_saved_key = self.has_api_key and not cleaned_data.get('clear_api_key')
        if provider_type in AI_PROVIDER_TYPES_REQUIRING_KEY and not cleaned_data.get('api_key') and not keeps_saved_key:
            self.add_error('api_key', 'An API key is required for this provider.')

        return cleaned_data
