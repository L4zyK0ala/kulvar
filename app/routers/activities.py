from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..activity_display import (
    activity_icon,
    activity_label,
    activity_stats,
    format_duration,
    format_pace,
    group_activities,
    is_running,
    monthly_running_summary,
    running_activities,
    weekly_running_distance,
)
from ..db import get_db
from ..deps import get_current_profile
from .. import feedback
from ..models import Activity, Profile, StrengthWorkout
from ..streams import compute_splits, find_sibling_with_stream, load_stream_data, stream_summary
from ..templating import templates

router = APIRouter()


@router.get("/activities")
def list_activities(
    request: Request,
    source: str | None = None,
    tab: str = "all",
    db: Session = Depends(get_db),
    current: Profile | None = Depends(get_current_profile),
):
    profiles = db.query(Profile).order_by(Profile.created_at).all()
    if current is None:
        return templates.TemplateResponse(request, "profile_picker.html", {"profiles": profiles})

    if tab == "running":
        since = utcnow() - dt.timedelta(days=180)
        runs = running_activities(db, current.id, since)
        return templates.TemplateResponse(
            request,
            "activities.html",
            {
                "tab": "running",
                "profiles": profiles,
                "active_profile": current,
                "running_summary": monthly_running_summary(db, current.id),
                "weekly": weekly_running_distance(db, current.id, weeks=10),
                "runs_feed": group_activities(runs)[:20],
            },
        )

    query = db.query(Activity).filter(Activity.profile_id == current.id)
    if source:
        query = query.filter(Activity.source == source)
    activities = query.order_by(Activity.start_time.desc()).limit(200).all()

    return templates.TemplateResponse(
        request,
        "activities.html",
        {
            "tab": "all",
            "feed": group_activities(activities),
            "active_profile": current,
            "source": source,
            "profiles": profiles,
        },
    )


@router.get("/activities/{activity_id}")
def activity_detail(
    activity_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current: Profile | None = Depends(get_current_profile),
):
    profiles = db.query(Profile).order_by(Profile.created_at).all()
    if current is None:
        return templates.TemplateResponse(request, "profile_picker.html", {"profiles": profiles})

    activity = db.get(Activity, activity_id)
    if activity is None or activity.profile_id != current.id:
        raise HTTPException(status_code=404, detail="Aktivite bulunamadı")

    stream_source = find_sibling_with_stream(db, activity)
    data = load_stream_data(stream_source.stream) if stream_source else None
    summary = stream_summary(data)
    splits = compute_splits(data, split_m=1000.0) if data else []

    map_points_json = None
    elevation_series = None
    hr_series = None
    if data:
        if data.get("latlng"):
            map_points_json = json.dumps(data["latlng"])
        distances = data.get("distance")
        if distances:
            labels = [round(d / 1000, 2) for d in distances]
            if data.get("altitude"):
                elevation_series = {"labels": labels, "data": data["altitude"]}
            if data.get("heartrate"):
                hr_series = {"labels": labels, "data": data["heartrate"]}

    stream_from_other_source = (
        stream_source.source if stream_source is not None and stream_source.id != activity.id else None
    )

    return templates.TemplateResponse(
        request,
        "activity_detail.html",
        {
            "profiles": profiles,
            "active_profile": current,
            "activity": activity,
            "icon": activity_icon(activity.source, activity.activity_type),
            "label": activity_label(activity.source, activity.activity_type),
            "stats": activity_stats(activity),
            "duration_str": format_duration(activity.duration_s),
            "pace": format_pace(activity.distance_m, activity.duration_s)
            if is_running(activity.source, activity.activity_type)
            else None,
            "summary": summary,
            "splits": splits,
            "map_points_json": map_points_json,
            "elevation_series": elevation_series,
            "hr_series": hr_series,
            "stream_from_other_source": stream_from_other_source,
            "rpe": {"kind": "act", "id": activity.id, "fb": feedback.find(db, current.id, feedback.refs_for_activity(db, activity))},
        },
    )


@router.post("/feedback/{profile_id}")
def save_feedback(profile_id: str, kind: str = Form(...), obj_id: str = Form(...), rpe: str = Form(""), note: str = Form(""),
                  back: str = Form("/dashboard"), db: Session = Depends(get_db)):
    """Antrenman sonrası his: RPE (1–10) ve not. Rakama basmak puanı, "Kaydet" yalnız notu kaydeder."""
    profile = db.get(Profile, profile_id)
    target = back if back.startswith("/") and not back.startswith("//") else "/dashboard"
    if profile is None:
        return RedirectResponse(target, status_code=303)
    if kind == "act":
        activity = db.get(Activity, obj_id)
        if activity is None or activity.profile_id != profile_id:
            return RedirectResponse(target, status_code=303)
        refs, day = feedback.refs_for_activity(db, activity), activity.start_time.date()
        minutes, title = (activity.duration_s or 0) / 60 or None, activity_label(activity.source, activity.activity_type)
    else:
        workout = db.get(StrengthWorkout, obj_id)
        if workout is None or workout.profile_id != profile_id:
            return RedirectResponse(target, status_code=303)
        refs, day, minutes, title = feedback.refs_for_workout(db, workout), workout.start_time.date(), (workout.duration_s or 0) / 60 or None, workout.title
    feedback.save(db, profile, refs, day, minutes, title, int(rpe) if rpe.isdigit() else None, note)
    return RedirectResponse(target + "#rpe", status_code=303)
