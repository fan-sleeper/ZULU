import os

from collections import Counter
from django.db.models import Count
from datetime import date, timedelta

from django.db.models import Q
from django.utils.dateparse import parse_date
from django.db.models.functions import TruncDay, TruncWeek, TruncMonth
from django.http import JsonResponse

from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from django.conf import settings
from django.http import JsonResponse

from core.models import Alert

def db_info(request):
    return JsonResponse({
        "ENGINE": settings.DATABASES["default"]["ENGINE"],
        "NAME": settings.DATABASES["default"]["NAME"],
        "HOST": settings.DATABASES["default"].get("HOST"),
    })

common_filter_parameters = [
    openapi.Parameter(
        "id",
        openapi.IN_QUERY,
        description="Filter by external alert identifier",
        type=openapi.TYPE_STRING,
    ),
    openapi.Parameter(
        "from",
        openapi.IN_QUERY,
        description="Start date in YYYY-MM-DD format",
        type=openapi.TYPE_STRING,
    ),
    openapi.Parameter(
        "to",
        openapi.IN_QUERY,
        description="End date in YYYY-MM-DD format",
        type=openapi.TYPE_STRING,
    ),
    openapi.Parameter(
        "disease",
        openapi.IN_QUERY,
        description="Filter by disease name. Can be supplied multiple times.",
        type=openapi.TYPE_STRING,
    ),
    openapi.Parameter(
        "species",
        openapi.IN_QUERY,
        description="Filter by affected species. Can be supplied multiple times.",
        type=openapi.TYPE_STRING,
    ),
    openapi.Parameter(
        "region",
        openapi.IN_QUERY,
        description="Filter by geographic region. Can be supplied multiple times.",
        type=openapi.TYPE_STRING,
    ),
    openapi.Parameter(
        "location",
        openapi.IN_QUERY,
        description="Filter by location text. Can be supplied multiple times.",
        type=openapi.TYPE_STRING,
    ),
]


def filter_alerts(params, default_days=365):
    alert_id = params.get("id")
    from_date = params.get("from")
    to_date = params.get("to")
    diseases = params.getlist("disease")
    species = params.getlist("species")
    regions = params.getlist("region")
    locations = params.getlist("location")

    today = date.today()

    if to_date:
        to_date = parse_date(to_date)
    else:
        to_date = today

    if from_date:
        from_date = parse_date(from_date)
    else:
        from_date = to_date - timedelta(days=default_days)

    query_set = Alert.objects.all().order_by("-date")

    if alert_id:
        query_set = query_set.filter(external_id=alert_id)

    if from_date:
        query_set = query_set.filter(date__gte=from_date)

    if to_date:
        query_set = query_set.filter(date__lte=to_date)

    if diseases:
        disease_query = Q()
        for d in diseases:
            disease_query |= Q(diseases__icontains=d)
        query_set = query_set.filter(disease_query)

    if species:
        species_query = Q()
        for s in species:
            species_query |= Q(species__icontains=s)
        query_set = query_set.filter(species_query)

    if regions:
        region_query = Q()
        for r in regions:
            region_query |= Q(regions__icontains=r)
        query_set = query_set.filter(region_query)

    if locations:
        location_query = Q()
        for loc in locations:
            location_query |= Q(locations__icontains=loc)
        query_set = query_set.filter(location_query)

    return query_set, from_date, to_date


def serialise_alert(alert):
    return {
        "id": alert.external_id,
        "date": alert.date.isoformat(),
        "title": alert.title,
        "disease": alert.diseases,
        "species": alert.species,
        "region": alert.regions,
        "location": alert.locations,
    }


def home(request):
    return JsonResponse({"message": "Zulu API is running"})


@swagger_auto_schema(
    method="get",
    operation_description="Return outbreak alert counts grouped by region"
    "using the same filters as the alerts endpoint.",
    tags=["stats"],
    manual_parameters=common_filter_parameters,
    responses={200: "Region summary returned successfully."},
)
@api_view(["GET"])
def stats_regions(request):
    query_set, from_date, to_date = filter_alerts(
        request.query_params,
        default_days=30,
    )

    requested_regions = request.query_params.getlist("region")
    requested_regions_lower = {r.lower() for r in requested_regions if r}

    region_counter = Counter()

    for alert in query_set:
        for region in alert.regions or []:
            if not region:
                continue

            if requested_regions_lower:
                if region.lower() in requested_regions_lower:
                    region_counter[region] += 1
            else:
                region_counter[region] += 1

    by_region = [
        {"region": region, "count": count}
        for region, count in sorted(
            region_counter.items(),
            key=lambda x: (-x[1], x[0]),
        )
    ]

    return Response(
        {
            "from": from_date.isoformat() if from_date else None,
            "to": to_date.isoformat() if to_date else None,
            "by_region": by_region,
        },
        status=status.HTTP_200_OK,
    )


@swagger_auto_schema(
    method="get",
    operation_description=(
        "Return the most frequently reported diseases using the same filters "
        "as the alerts endpoint. Set include_ai=true to optionally enrich "
        "the top diseases with AI-generated severity information."
    ),
    tags=["stats"],
    manual_parameters=[
        *common_filter_parameters,
        openapi.Parameter(
            "include_ai",
            openapi.IN_QUERY,
            description="Set to true to include AI-generated"
            "disease severity enrichment.",
            type=openapi.TYPE_STRING,
            enum=["true", "false"],
        ),
        openapi.Parameter(
            "ai_limit",
            openapi.IN_QUERY,
            description="Maximum number of diseases to send"
            "to AI when include_ai=true.",
            type=openapi.TYPE_INTEGER,
        ),
    ],
    responses={200: "Disease summary returned successfully."},
)
@api_view(["GET"])
def stats_diseases(request):
    query_set, from_date, to_date = filter_alerts(
        request.query_params,
        default_days=30,
    )

    requested_diseases = request.query_params.getlist("disease")
    requested_diseases_lower = {d.lower() for d in requested_diseases if d}

    include_ai = request.query_params.get("include_ai", "false").lower() == "true"

    try:
        ai_limit = int(request.query_params.get("ai_limit", 10))
    except ValueError:
        return Response(
            {"error": "ai_limit must be an integer"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if ai_limit < 1:
        ai_limit = 1
    if ai_limit > 20:
        ai_limit = 20

    disease_counter = Counter()

    for alert in query_set:
        for disease in alert.diseases or []:
            if not disease:
                continue

            if requested_diseases_lower:
                if disease.lower() in requested_diseases_lower:
                    disease_counter[disease] += 1
            else:
                disease_counter[disease] += 1

    by_disease = [
        {"disease": disease, "count": count}
        for disease, count in sorted(
            disease_counter.items(),
            key=lambda x: (-x[1], x[0]),
        )
    ]

    response_data = {
        "from": from_date.isoformat() if from_date else None,
        "to": to_date.isoformat() if to_date else None,
        "by_disease": by_disease,
        "ai_included": include_ai,
    }

    if include_ai:
        try:
            from core.ai_service.disease_severity import (
                generate_disease_severity_entry,
            )

            ai_input = {
                "from": response_data["from"],
                "to": response_data["to"],
                "by_disease": by_disease[:ai_limit],
            }

            update_result = generate_disease_severity_entry(
                response_raw=ai_input,
                api_key=os.getenv("GEMINI_API_KEY"),
            )

            response_data["ai_limit"] = ai_limit
            response_data["new_disease"] = update_result.get("new_disease", [])
            response_data["updated_disease"] = update_result.get(
                "updated_disease", []
            )

            if update_result.get("errors"):
                response_data["severity_update_errors"] = update_result["errors"]

        except Exception as e:
            response_data["severity_update_errors"] = [str(e)]

    return Response(response_data, status=status.HTTP_200_OK)


@swagger_auto_schema(
    method="get",
    operation_description="Retrieve outbreak alerts with optional filters.",
    tags=["alerts"],
    manual_parameters=common_filter_parameters,
    responses={200: "List of matching alerts returned successfully."},
)
@api_view(["GET"])
def get_alerts(request):
    query_set, from_date, to_date = filter_alerts(request.query_params)

    alerts_out = [serialise_alert(alert) for alert in query_set]

    return Response(
        {
            "from": from_date.isoformat() if from_date else None,
            "to": to_date.isoformat() if to_date else None,
            "alerts": alerts_out,
        },
        status=status.HTTP_200_OK,
    )


timeseries_parameters = [
    openapi.Parameter(
        "interval",
        openapi.IN_QUERY,
        description="Aggregation interval: day, week, or month",
        type=openapi.TYPE_STRING,
        enum=["day", "week", "month"],
    ),
    *common_filter_parameters,
]


@swagger_auto_schema(
    method="get",
    operation_description="Return alert counts aggregated over time"
    "using the same filters as the alerts endpoint.",
    tags=["stats"],
    manual_parameters=timeseries_parameters,
    responses={
        200: "Timeseries summary returned successfully.",
        400: "Invalid interval. Must be day, week, or month.",
    }
)
@api_view(["GET"])
def stats_timeseries(request):
    interval = request.query_params.get("interval", "day")

    trunc_map = {
        "day": TruncDay,
        "week": TruncWeek,
        "month": TruncMonth,
    }

    if interval not in trunc_map:
        return Response(
            {"error": "Invalid interval"}, status=status.HTTP_400_BAD_REQUEST
        )

    queryset, from_date, to_date = filter_alerts(request.query_params, default_days=90)

    trunc_func = trunc_map[interval]

    series = (
        queryset.annotate(period=trunc_func("date"))
        .values("period")
        .annotate(count=Count("id"))
        .order_by("period")
    )

    results = [
        {"period": row["period"].isoformat(), "count": row["count"]} for row in series
    ]

    return Response(
        {
            "interval": interval,
            "from": from_date.isoformat() if from_date else None,
            "to": to_date.isoformat() if to_date else None,
            "results": results,
        },
        status=status.HTTP_200_OK,
    )


def serialise_alert_for_ai(alert):
    return {
        "fields": {
            "external_id": alert.external_id,
            "date": alert.date.isoformat(),
            "title": alert.title,
            "regions": alert.regions,
            "diseases": alert.diseases,
            "species": alert.species,
            "locations": alert.locations,
        }
    }


@swagger_auto_schema(
    method="get",
    operation_description="Generate an AI summary for a region or location.",
    tags=["summary"],
    manual_parameters=[
        openapi.Parameter(
            "location",
            openapi.IN_QUERY,
            description="Location string to summarise",
            type=openapi.TYPE_STRING,
            required=True,
        ),
        openapi.Parameter(
            "window",
            openapi.IN_QUERY,
            description="Optional time window (7day, 1month, 3month, 6month)",
            type=openapi.TYPE_STRING,
            enum=["7day", "1month", "3month", "6month"],
        ),
        openapi.Parameter(
            "from",
            openapi.IN_QUERY,
            description="Start date in YYYY-MM-DD format",
            type=openapi.TYPE_STRING,
        ),
        openapi.Parameter(
            "to",
            openapi.IN_QUERY,
            description="End date in YYYY-MM-DD format",
            type=openapi.TYPE_STRING,
        ),
    ],
    responses={200: "Summary generated successfully."},
)
@api_view(["GET"])
def region_summary_view(request):
    from core.ai_service.region_summary import generate_summary_entry

    location = request.query_params.get("location")
    window = request.query_params.get("window")
    raw_from = request.query_params.get("from")
    raw_to = request.query_params.get("to")
    limit = int(request.query_params.get("limit", 200))

    if not location:
        return Response(
            {"error": "location_chain or location_str is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    start_date = parse_date(raw_from) if raw_from else None
    end_date = parse_date(raw_to) if raw_to else None

    if raw_from and start_date is None:
        return Response(
            {"error": "invalid from date, expected YYYY-MM-DD"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if raw_to and end_date is None:
        return Response(
            {"error": "invalid to date, expected YYYY-MM-DD"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    query_set = Alert.objects.all().order_by("-date")[:limit]
    database = [serialise_alert_for_ai(alert) for alert in query_set]

    try:
        result = generate_summary_entry(
            database=database,
            location_str=location,
            window=window,
            start_date=start_date,
            end_date=end_date,
        )
        return Response(result, status=status.HTTP_200_OK)

    except ValueError as e:
        return Response(
            {"error": str(e)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except RuntimeError as e:
        return Response(
            {"error": "AI summary generation failed", "detail": str(e)},
            status=status.HTTP_502_BAD_GATEWAY,
        )
