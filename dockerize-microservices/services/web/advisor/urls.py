"""
URL configuration for the advisor app.
"""
from django.urls import path
from . import explorer_views, views

app_name = 'advisor'

urlpatterns = [
    # Home / Dashboard
    path('', views.home, name='home'),
    path('healthz/', views.healthz, name='healthz'),

    # Connection management
    path('connections/', views.connection_list, name='connection_list'),
    path('connections/add/', views.connection_add, name='connection_add'),
    path('connections/<int:pk>/edit/', views.connection_edit, name='connection_edit'),
    path('connections/<int:pk>/delete/', views.connection_delete, name='connection_delete'),
    path('connections/<int:pk>/test/', views.connection_test, name='connection_test'),

    # Explorer: object tree, panels and the query tool
    path('explorer/', explorer_views.explorer, name='explorer'),
    path('explorer/panel/', explorer_views.explorer_panel, name='explorer_panel'),
    path('api/explorer/tree/', explorer_views.api_explorer_tree, name='api_explorer_tree'),
    # Older links (Results, Connections) open the matching place in the Explorer
    path('explore/', explorer_views.explorer_pick, name='explorer_pick'),
    path('connections/<int:pk>/explore/', explorer_views.explorer_overview, name='explorer_overview'),
    path('connections/<int:pk>/explore/<str:schema>/<str:table>/', explorer_views.explorer_table,
         name='explorer_table'),
    path('connections/<int:pk>/sql/', explorer_views.explorer_console, name='explorer_console'),

    # Test data generator
    path('explorer/generate/', explorer_views.datagen_page, name='datagen'),
    path('api/datagen/plan/', explorer_views.api_datagen_plan, name='api_datagen_plan'),
    path('api/datagen/start/', explorer_views.api_datagen_start, name='api_datagen_start'),
    path('api/datagen/jobs/<str:job_id>/', explorer_views.api_datagen_job, name='api_datagen_job'),
    path('api/datagen/jobs/<str:job_id>/cancel/', explorer_views.api_datagen_cancel, name='api_datagen_cancel'),

    # Query analysis
    path('analyze/<int:connection_id>/', views.analyze_query, name='analyze_query'),
    path('results/<int:query_id>/', views.view_results, name='view_results'),
    path('results/<int:query_id>/pdf/', views.download_pdf, name='download_pdf'),

    # History
    path('history/', views.query_history, name='query_history'),
    path('history/<int:pk>/delete/', views.history_delete, name='history_delete'),

    # Test cases (the benchmark suite)
    path('test-cases/', views.test_cases, name='test_cases'),
    path('api/test-cases/run/', views.api_test_case, name='api_test_case'),

    # AI Settings (always reachable, even while the app is locked)
    path('settings/ai/', views.ai_settings, name='ai_settings'),
    path('settings/ai/add/', views.ai_provider_add, name='ai_provider_add'),
    path('settings/ai/test/', views.ai_provider_test, name='ai_provider_test'),
    path('settings/ai/check-all/', views.ai_check_all, name='ai_check_all'),
    path('settings/ai/<int:pk>/edit/', views.ai_provider_edit, name='ai_provider_edit'),
    path('settings/ai/<int:pk>/delete/', views.ai_provider_delete, name='ai_provider_delete'),
    path('settings/ai/<int:pk>/check/', views.ai_provider_check, name='ai_provider_check'),

    # API endpoints for AJAX
    path('api/analyze/', views.api_analyze, name='api_analyze'),
    path('api/analyze/progress/<str:progress_id>/', views.api_analyze_progress, name='api_analyze_progress'),
    path('api/connections/<int:pk>/sql/', explorer_views.api_explorer_query, name='api_explorer_query'),
]
