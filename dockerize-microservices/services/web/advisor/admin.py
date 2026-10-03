from django.contrib import admin
from .models import Connection, QueryHistory, Recommendation


@admin.register(Connection)
class ConnectionAdmin(admin.ModelAdmin):
    list_display = ['name', 'database', 'port', 'ssl_mode', 'created_at']
    search_fields = ['name', 'database']
    list_filter = ['ssl_mode', 'created_at']


@admin.register(QueryHistory)
class QueryHistoryAdmin(admin.ModelAdmin):
    list_display = ['id', 'connection', 'analysis_status', 'original_execution_time', 'created_at']
    list_filter = ['analysis_status', 'created_at']
    search_fields = ['original_query']
    raw_id_fields = ['connection']


@admin.register(Recommendation)
class RecommendationAdmin(admin.ModelAdmin):
    list_display = ['id', 'query_history', 'recommendation_type', 'improvement_percentage', 'rank']
    list_filter = ['recommendation_type']
    raw_id_fields = ['query_history']
