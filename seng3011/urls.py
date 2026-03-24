from django.contrib import admin
from django.urls import path, include, re_path
from rest_framework import permissions
from drf_yasg.views import get_schema_view
from drf_yasg import openapi

schema_view = get_schema_view(
    openapi.Info(
        title="Zulu Health Alerts API",
        default_version="v1",
        description="API for querying structured public health alert data",
    ),
    public=True,
    permission_classes=(permissions.AllowAny,),
    url="https://mbkgairwrk.ap-southeast-2.awsapprunner.com",
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("core.urls")),

    re_path(
        r"^swagger(?P<format>\.json|\.yaml)$",
        schema_view.without_ui(cache_timeout=0),
        name="schema-json",
    ),
    path(
        "swagger/",
        schema_view.with_ui("swagger", cache_timeout=0),
        name="schema-swagger-ui",
    ),
    path(
        "redoc/",
        schema_view.with_ui("redoc", cache_timeout=0),
        name="schema-redoc",
    ),
]
