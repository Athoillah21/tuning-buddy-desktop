"""
Formatting for catalog numbers: estimated row counts and relative sizes.
"""
from django import template
from django.utils import timezone
from django.utils.dateparse import parse_datetime

register = template.Library()


@register.filter
def approx_rows(value):
    """1234567 -> '1.2M'. Row counts from the catalog are estimates, so no false precision."""
    if value is None or value == '':
        return '—'
    try:
        value = float(value)
    except (TypeError, ValueError):
        return value
    for limit, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'k')):
        if value >= limit:
            return f"{value / limit:.1f}{suffix}"
    return f"{value:.0f}"


@register.filter
def share_of(value, total):
    """Width of a size bar: value as a percentage of total, 0-100."""
    try:
        value, total = float(value or 0), float(total or 0)
    except (TypeError, ValueError):
        return 0
    if total <= 0:
        return 0
    return max(min(round(value * 100 / total, 1), 100), 0)


@register.filter
def iso_datetime(value):
    """'2026-09-24T12:10:58.123+10:00' -> '2026-09-24 12:10' in the app's time zone."""
    if not value:
        return 'never'
    parsed = parse_datetime(str(value))
    if parsed is None:
        return value
    if timezone.is_aware(parsed):
        parsed = timezone.localtime(parsed)
    return parsed.strftime('%Y-%m-%d %H:%M')


@register.filter
def number(value):
    """1234567 -> '1,234,567'; None -> '—'."""
    if value is None or value == '':
        return '—'
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return value


@register.filter
def get_item(mapping, key):
    """mapping[key] in a template; None when missing."""
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.simple_tag
def explorer_href(**params):
    """A link to a place in the Explorer: {% explorer_href c=1 db='shop' kind='table' name='orders' %}."""
    from ..explorer_views import explorer_url
    return explorer_url(**params)


@register.filter
def duration(seconds):
    """93.4 -> '1m 33s'; None -> '—'."""
    if seconds is None or seconds == '':
        return '—'
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return seconds
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


@register.filter
def ms(value):
    """A duration in milliseconds, as the Test cases page shows it: 0.041 ms, 201.8 ms, 1.24 s."""
    if value is None or value == '':
        return '–'
    try:
        value = float(value)
    except (TypeError, ValueError):
        return value
    if value >= 1000:
        return f"{value / 1000:.2f} s"
    return f"{value:.3f} ms" if value < 10 else f"{value:.1f} ms"


@register.filter
def signed_percent(value):
    """12.34 -> '+12.3%'; None -> '–'."""
    if value is None or value == '':
        return '–'
    try:
        value = float(value)
    except (TypeError, ValueError):
        return value
    return f"{'+' if value >= 0 else ''}{value:.1f}%"
