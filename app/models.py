from __future__ import annotations

import uuid

from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from .clock import utcnow
from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


class Profile(Base):
    __tablename__ = "profiles"

    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    color = Column(String, nullable=False, default="#2563eb")
    weight_goal_kg = Column(Float, nullable=True)
    height_cm = Column(Float, nullable=True)
    gender = Column(String, nullable=True)  # female | male
    avatar_path = Column(String, nullable=True)  # static/ altına göreli yol, örn. uploads/avatars/<dosya>.jpg
    # Profile özel Strava API uygulaması (Fernet ile şifreli {"client_id", "client_secret"}).
    # Strava yeni uygulamalara 1 bağlı sporcu kapasitesi veriyor; ikinci kişi .env'deki ortak
    # uygulamaya bağlanamadığı için kendi uygulamasını oluşturup burada kullanabiliyor.
    strava_app_encrypted = Column(Text, nullable=True)
    coach_instructions = Column(Text, nullable=True)  # Kulvar Koç sohbeti için profile özel talimatlar
    coach_notes = Column(Text, nullable=True)  # koçun kendi tuttuğu güncel özet (haftalık kendisi günceller)
    coach_notes_updated_at = Column(DateTime, nullable=True)
    strength_source = Column(String, nullable=True)  # kuvvet kaydının kaynağı: hevy | kulvar (boşsa Hevy bağlıysa hevy)
    home_equipment = Column(Text, nullable=True)  # JSON {"dumbbell": [5, 10], "kettlebell": [12]} — evdeki ağırlıklar (kg)
    meds_eval_status = Column(String, nullable=True)  # running | failed | done — takviye değerlendirmesi (arka planda)
    meds_eval_summary = Column(Text, nullable=True)
    meds_eval_at = Column(DateTime, nullable=True)
    telegram_chat_id = Column(String, nullable=True)  # bu profile bağlı Telegram grubu
    telegram_link_code = Column(String, nullable=True)  # grupta /bagla KOD ile eşleştirme
    telegram_bot_encrypted = Column(Text, nullable=True)  # profilin kendi botu: şifreli {token, username}
    # Normal dönem haftalık düzeni: Pzt..Paz için "run" | "long" | "strength" | "rest" | "optional" (virgülle).
    # Boşsa training.DEFAULT_WEEK geçerli. Yarış dönemlerinde koç bu düzeni yarış fazına göre değiştirebilir.
    week_template = Column(String, nullable=True)
    # Nabız profili (app/heart.py): Garmin'den günde bir yenilenir; hr_profile_source='manual' ise LTHR elle girilmiştir
    lthr = Column(Integer, nullable=True)
    lthr_date = Column(Date, nullable=True)
    hr_max = Column(Integer, nullable=True)
    hr_rest = Column(Integer, nullable=True)
    garmin_hr_zones = Column(Text, nullable=True)  # {"floors": [Z1..Z5 taban], "method": "HR_MAX"}
    hr_profile_source = Column(String, nullable=True)  # garmin | manual
    hr_profile_updated_at = Column(DateTime, nullable=True)
    language = Column(String, nullable=True)  # arayüz, koç ve Telegram dili: tr | en (boşsa tr)
    created_at = Column(DateTime, default=utcnow)

    credentials = relationship("Credential", back_populates="profile", cascade="all, delete-orphan")
    activities = relationship("Activity", back_populates="profile", cascade="all, delete-orphan")
    daily_metrics = relationship("DailyMetric", back_populates="profile", cascade="all, delete-orphan")
    nutrition_logs = relationship("NutritionLog", back_populates="profile", cascade="all, delete-orphan")
    planned_workouts = relationship("PlannedWorkout", back_populates="profile", cascade="all, delete-orphan")
    body_measurements = relationship("BodyMeasurement", back_populates="profile", cascade="all, delete-orphan")


class Credential(Base):
    """Bir profilin bir veri kaynağına (garmin/strava/hevy/mfp) bağlantı bilgisi.

    encrypted_payload, crypto.encrypt_payload() ile şifrelenmiş bir JSON string'idir
    (garth session, strava refresh token, hevy api key, mfp email+şifre gibi).
    """

    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("profile_id", "source", name="uq_credential_profile_source"),)

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False)
    source = Column(String, nullable=False)
    encrypted_payload = Column(Text, nullable=False)
    last_synced_at = Column(DateTime, nullable=True)
    last_sync_status = Column(String, nullable=True)
    last_sync_message = Column(Text, nullable=True)
    failing_since = Column(DateTime, nullable=True)  # ilk başarısız senkron (düzelince temizlenir)
    alert_sent_at = Column(DateTime, nullable=True)  # Telegram uyarısı gönderildi mi

    profile = relationship("Profile", back_populates="credentials")


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_activity_source_external_id"),)

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False)
    source = Column(String, nullable=False)
    external_id = Column(String, nullable=False)
    activity_type = Column(String, nullable=True)
    start_time = Column(DateTime, nullable=False)
    duration_s = Column(Float, nullable=True)
    distance_m = Column(Float, nullable=True)
    calories = Column(Float, nullable=True)
    avg_hr = Column(Float, nullable=True)
    max_hr = Column(Float, nullable=True)
    elevation_gain_m = Column(Float, nullable=True)
    raw_json = Column(Text, nullable=True)

    profile = relationship("Profile", back_populates="activities")
    stream = relationship("ActivityStream", back_populates="activity", uselist=False, cascade="all, delete-orphan")


class ActivityStream(Base):
    """Bir aktivitenin GPS/HR/kadans zaman serisi verisi (şu an sadece Strava sağlıyor).

    data_json, aynı index'e hizalanmış eş-uzunlukta dizilerden oluşan bir JSON sözlüğüdür:
    {"time": [...], "distance": [...], "latlng": [[lat,lon],...], "altitude": [...],
     "heartrate": [...], "cadence": [...], "watts": [...]}
    """

    __tablename__ = "activity_streams"

    id = Column(String, primary_key=True, default=_uuid)
    activity_id = Column(String, ForeignKey("activities.id"), nullable=False, unique=True)
    point_count = Column(Integer, nullable=True)
    data_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow)

    activity = relationship("Activity", back_populates="stream")


class DailyMetric(Base):
    __tablename__ = "daily_metrics"
    __table_args__ = (
        UniqueConstraint("profile_id", "date", "source", name="uq_daily_metric_profile_date_source"),
    )

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False)
    date = Column(Date, nullable=False)
    source = Column(String, nullable=False)
    steps = Column(Integer, nullable=True)
    steps_goal = Column(Integer, nullable=True)
    resting_hr = Column(Integer, nullable=True)
    sleep_score = Column(Integer, nullable=True)
    sleep_duration_s = Column(Float, nullable=True)
    body_battery_min = Column(Integer, nullable=True)
    body_battery_max = Column(Integer, nullable=True)
    stress_avg = Column(Integer, nullable=True)
    weight_kg = Column(Float, nullable=True)

    profile = relationship("Profile", back_populates="daily_metrics")


class NutritionLog(Base):
    __tablename__ = "nutrition_logs"
    __table_args__ = (
        UniqueConstraint("profile_id", "date", "source", name="uq_nutrition_profile_date_source"),
    )

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False)
    date = Column(Date, nullable=False)
    source = Column(String, nullable=False, default="mfp")
    calories = Column(Float, nullable=True)
    protein_g = Column(Float, nullable=True)
    carbs_g = Column(Float, nullable=True)
    fat_g = Column(Float, nullable=True)
    calories_goal = Column(Float, nullable=True)
    protein_goal_g = Column(Float, nullable=True)
    carbs_goal_g = Column(Float, nullable=True)
    fat_goal_g = Column(Float, nullable=True)
    sodium_mg = Column(Float, nullable=True)
    sodium_goal_mg = Column(Float, nullable=True)
    sugar_g = Column(Float, nullable=True)
    sugar_goal_g = Column(Float, nullable=True)
    fiber_g = Column(Float, nullable=True)
    fiber_goal_g = Column(Float, nullable=True)
    cholesterol_mg = Column(Float, nullable=True)
    cholesterol_goal_mg = Column(Float, nullable=True)

    profile = relationship("Profile", back_populates="nutrition_logs")


class BodyMeasurement(Base):
    """Vücut ölçümü (kilo, yağ oranı, çevre ölçümleri) — şu an Hevy'nin
    Measurements bölümünden geliyor, ileride başka kaynaklar da eklenebilir.
    """

    __tablename__ = "body_measurements"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_body_measurement_source_external_id"),
    )

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False)
    source = Column(String, nullable=False)
    external_id = Column(String, nullable=False)
    date = Column(Date, nullable=False)
    weight_kg = Column(Float, nullable=True)
    lean_mass_kg = Column(Float, nullable=True)
    fat_percent = Column(Float, nullable=True)
    neck_cm = Column(Float, nullable=True)
    shoulder_cm = Column(Float, nullable=True)
    chest_cm = Column(Float, nullable=True)
    left_bicep_cm = Column(Float, nullable=True)
    right_bicep_cm = Column(Float, nullable=True)
    left_forearm_cm = Column(Float, nullable=True)
    right_forearm_cm = Column(Float, nullable=True)
    abdomen_cm = Column(Float, nullable=True)
    waist_cm = Column(Float, nullable=True)
    hips_cm = Column(Float, nullable=True)
    left_thigh_cm = Column(Float, nullable=True)
    right_thigh_cm = Column(Float, nullable=True)
    left_calf_cm = Column(Float, nullable=True)
    right_calf_cm = Column(Float, nullable=True)

    profile = relationship("Profile", back_populates="body_measurements")


class PlannedWorkout(Base):
    """Claude Spor Koçu'nun önerdiği ve kullanıcının elle girdiği yaklaşan antrenman.

    garmin_workout_id doluysa Garmin Connect takvimine gönderilmiş demektir.
    """

    __tablename__ = "planned_workouts"

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False)
    date = Column(Date, nullable=False)
    title = Column(String, nullable=False)
    workout_type = Column(String, nullable=False)  # running | cycling | walking | swimming | strength | other
    target_distance_m = Column(Float, nullable=True)
    target_duration_s = Column(Float, nullable=True)
    target_hr_min = Column(Integer, nullable=True)  # nabız hedefi (bpm); sadece tavan verildiyse boş
    target_hr_max = Column(Integer, nullable=True)  # Garmin saati bu tavanı aşınca uyarır
    notes = Column(Text, nullable=True)
    garmin_workout_id = Column(String, nullable=True)
    garmin_error = Column(Text, nullable=True)
    status = Column(String, nullable=True)  # None=planlandı | done | skipped (kullanıcı bildirdi)
    routine_id = Column(String, nullable=True)  # kuvvet günleri: StrengthRoutine.id (otomatik eşlenir, değiştirilebilir)
    status_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    profile = relationship("Profile", back_populates="planned_workouts")


class CoachThread(Base):
    """Bir profilin Kulvar Koç sohbeti (Claude API). Her profilin sohbetleri ayrıdır."""

    __tablename__ = "coach_threads"

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    snapshot_hash = Column(String, nullable=True)  # sohbete en son eklenen veri özetinin özeti
    created_at = Column(DateTime, default=utcnow)

    messages = relationship(
        "CoachMessage", back_populates="thread", cascade="all, delete-orphan", order_by="CoachMessage.seq"
    )


class CoachMessage(Base):
    """Sohbet mesajı. content_json API'ye aynen geri gönderilen içerik bloklarıdır (düşünme blokları
    dahil — Claude Opus 5.5'te bu bloklar konuşmaya bağlı olduğu için geçmiş hiç düzenlenmez,
    sadece sona eklenir). Asistan mesajlarında token kullanımı ve maliyet de saklanır."""

    __tablename__ = "coach_messages"

    id = Column(String, primary_key=True, default=_uuid)
    thread_id = Column(String, ForeignKey("coach_threads.id"), nullable=False, index=True)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    role = Column(String, nullable=False)  # user | system | assistant
    content_json = Column(Text, nullable=False)
    display_text = Column(Text, nullable=True)
    channel = Column(String, nullable=True)  # web | telegram | auto
    created_at = Column(DateTime, default=utcnow)

    model = Column(String, nullable=True)
    stop_reason = Column(String, nullable=True)
    request_id = Column(String, nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    cache_read_tokens = Column(Integer, nullable=True)
    cache_write_tokens = Column(Integer, nullable=True)
    cost_usd = Column(Float, nullable=True)

    thread = relationship("CoachThread", back_populates="messages")


class PlanSuggestion(Base):
    """Koçun önerdiği, Telegram'da onay bekleyen plan satırları. Onaylanınca PlannedWorkout
    olur ve Garmin'e gönderilir; reddedilirse sadece işaretlenir."""

    __tablename__ = "plan_suggestions"

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    plan_text = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="pending")  # pending | accepted | rejected
    result = Column(Text, nullable=True)
    telegram_message_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class HealthSchedule(Base):
    """Periyodik sağlık kontrolü: kan tahlili (6 ay), kardiyoloji (12 ay)."""

    __tablename__ = "health_schedules"
    __table_args__ = (UniqueConstraint("profile_id", "kind", name="uq_health_schedule_profile_kind"),)

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    kind = Column(String, nullable=False)  # blood | cardio
    interval_months = Column(Integer, nullable=False)
    last_date = Column(Date, nullable=True)
    last_reminded_at = Column(DateTime, nullable=True)


class HealthRecord(Base):
    """Tahlil/kardiyoloji belgesi veya doktor notu. Dosyalar static/ dışında saklanır (web'den
    doğrudan erişilemez). ai_summary Claude'un yorumu; coach_note koça giden kısa not."""

    __tablename__ = "health_records"

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    kind = Column(String, nullable=True)  # blood | cardio | doctor_note | other | None (Telegram'da tür seçimi bekliyor)
    record_date = Column(Date, nullable=True)
    file_path = Column(String, nullable=True)
    mime_type = Column(String, nullable=True)
    extra_files = Column(Text, nullable=True)  # aynı tahlilin ek belgeleri: JSON [{"path", "mime", "name"}]
    file_name = Column(String, nullable=True)  # ilk belgenin özgün adı
    telegram_group = Column(String, nullable=True)  # Telegram'da birlikte gönderilen dosyalar (media_group_id)
    analysis_status = Column(String, nullable=True)  # running | failed | None (bitti ya da hiç başlamadı)
    analysis_error = Column(Text, nullable=True)
    labs_extracted_at = Column(DateTime, nullable=True)  # sayısal değerler çıkarıldı (0 sonuç olsa da tekrar taranmaz)
    text_note = Column(Text, nullable=True)
    ai_summary = Column(Text, nullable=True)
    coach_note = Column(Text, nullable=True)
    cost_usd = Column(Float, nullable=True)
    source = Column(String, nullable=True)  # web | telegram
    created_at = Column(DateTime, default=utcnow)


class Race(Base):
    """Hedef yarış. A önceliği yarış dönemini (hazırlık → taper → toparlanma) başlatır;
    B kısa taper'lı ara yarış, C antrenman koşusu olarak ele alınır."""

    __tablename__ = "races"

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False)
    date = Column(Date, nullable=False)
    name = Column(String, nullable=False)
    distance_km = Column(Float, nullable=False)
    priority = Column(String, nullable=False, default="A")  # A | B | C
    goal = Column(String, nullable=True)  # ör. "1:50 altı" veya "rahat bitir"
    prep_weeks = Column(Integer, nullable=True)  # boşsa mesafeye göre varsayılan
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class InstructionRevision(Base):
    """Koç talimatları ve koç notlarındaki her değişiklik: geri alınabilsin diye tam önceki/sonraki metin.

    kind: instructions | notes. Koçun talimat önerileri status=pending başlar; kullanıcı onaylarsa
    applied, reddederse rejected olur. Koç notları ve kullanıcı düzenlemeleri doğrudan applied."""

    __tablename__ = "instruction_revisions"

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    kind = Column(String, nullable=False)  # instructions | notes
    source = Column(String, nullable=False)  # coach | user | revert
    status = Column(String, nullable=False, default="applied")  # pending | applied | rejected | stale
    old_snippet = Column(Text, nullable=True)  # öneride değişecek metin (boşsa sona ekleme)
    new_snippet = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)
    old_full = Column(Text, nullable=True)  # uygulanınca: önceki tam metin
    new_full = Column(Text, nullable=True)  # uygulanınca: sonraki tam metin
    telegram_message_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    decided_at = Column(DateTime, nullable=True)


class Medication(Base):
    """Sağlık sekmesindeki ilaç/takviye. kind: supplement | prescription.

    Doktorun verdiği ilacın dozu ve zamanları kullanıcının girdiğidir; koç bunları değiştirmez,
    yalnızca takviyeleri tahlillere göre değerlendirir (coach_* alanları)."""

    __tablename__ = "medications"

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=False, default="supplement")  # supplement | prescription
    status = Column(String, nullable=False, default="active")  # active | considering | stopped
    dose = Column(String, nullable=True)  # ör. "1 tablet", "1000 IU"
    times = Column(String, nullable=True)  # hatırlatma saatleri, virgülle: "08:00,20:00"
    days = Column(String, nullable=True)  # haftanın günleri (0=Pzt … 6=Paz), virgülle: "0,3"; boşsa her gün
    quantity = Column(Integer, nullable=True)  # kutudaki adet; bitiş tarihi buna göre hesaplanır
    instructions = Column(String, nullable=True)  # ör. "tok karnına"
    reminders = Column(Integer, nullable=False, default=1)  # 1: Telegram'dan hatırlat
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    coach_verdict = Column(String, nullable=True)  # needed | not_needed | ask_doctor
    coach_timing = Column(Text, nullable=True)
    coach_reason = Column(Text, nullable=True)
    coach_evaluated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    stopped_at = Column(DateTime, nullable=True)


class MedicationLog(Base):
    """Bir hatırlatma ve cevabı: pending → taken | skipped | missed (gün sonunda cevapsızsa)."""

    __tablename__ = "medication_logs"
    __table_args__ = (UniqueConstraint("medication_id", "scheduled_for", name="uq_medication_log_slot"),)

    id = Column(String, primary_key=True, default=_uuid)
    medication_id = Column(String, ForeignKey("medications.id"), nullable=False, index=True)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    scheduled_for = Column(DateTime, nullable=False)  # yerel saat
    status = Column(String, nullable=False, default="pending")  # pending | taken | skipped | missed
    snooze_until = Column(DateTime, nullable=True)
    telegram_message_id = Column(String, nullable=True)
    responded_at = Column(DateTime, nullable=True)


class StrengthRoutine(Base):
    """Profilin Hevy rutini (D1, D2 …): hareketler ve setler. Hevy senkronunda günde bir yenilenir."""

    __tablename__ = "strength_routines"
    __table_args__ = (UniqueConstraint("profile_id", "hevy_id", name="uq_strength_routine_profile_hevy"),)

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    hevy_id = Column(String, nullable=False)
    title = Column(String, nullable=False)
    exercises_json = Column(Text, nullable=False)  # [{template_id, title, rest_seconds, notes, sets: [{type, reps, weight_kg, duration_seconds}]}]
    notes = Column(Text, nullable=True)
    # Kulvar'da oluşturulan rutinlerde hevy_id "kulvar-…" ile başlar. Hevy'deki bir rutin Kulvar'a taşınınca
    # (Hevy'de karşılığı olmayan hareket eklendi) Hevy'deki kopyanın kimliği burada tutulur, yenilemede geri gelmez.
    replaces_hevy_id = Column(String, nullable=True)
    updated_at = Column(DateTime, default=utcnow)


class ExerciseMedia(Base):
    """Bir Hevy hareket şablonunun görseli, talimatı ve Garmin karşılığı (bir kez eşlenir, saklanır).

    frames: free-exercise-db'den indirilen başlangıç/bitiş kareleri (static/exercise/… altında);
    yoksa Hevy'nin küçük resmi kullanılır."""

    __tablename__ = "exercise_media"

    template_id = Column(String, primary_key=True)  # Hevy exercise_template_id
    title = Column(String, nullable=False)
    primary_muscle = Column(String, nullable=True)
    equipment = Column(String, nullable=True)
    thumbnail_url = Column(String, nullable=True)  # Hevy küçük resmi
    fedb_id = Column(String, nullable=True)  # free-exercise-db kimliği
    frames = Column(Text, nullable=True)  # JSON: ["exercise/<id>/0.jpg", "exercise/<id>/1.jpg"]
    instructions = Column(Text, nullable=True)  # JSON liste (İngilizce, kaynaktan)
    garmin_category = Column(String, nullable=True)
    garmin_name = Column(String, nullable=True)
    mapped_by = Column(String, nullable=True)  # auto | claude | none
    updated_at = Column(DateTime, default=utcnow)


class RoutineDraft(Base):
    """Koçun hazırladığı kuvvet rutini taslağı (ör. ev rutinleri). Onaylanınca Hevy'ye kaydedilir.

    status: running → ready | failed; ready → saving → saved | save_failed; discarded."""

    __tablename__ = "routine_drafts"

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    status = Column(String, nullable=False, default="running")
    params_json = Column(Text, nullable=True)  # {days, minutes, equipment: [...], weights, prefix, notes}
    routines_json = Column(Text, nullable=True)  # [{title, notes, exercises: [StrengthRoutine.exercises_json biçimi]}]
    summary = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    cost_usd = Column(Float, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    saved_at = Column(DateTime, nullable=True)


class StravaEnrichment(Base):
    """Strava kaydına Hevy ayrıntılarının yazılma durumu (aynı içerik tekrar yazılmasın).

    status: written (Kulvar yazdı) | hevy_copy (Hevy'nin kendi yüklemesi) | user_text (elle yazılmış açıklama)."""

    __tablename__ = "strava_enrichments"

    strava_id = Column(String, primary_key=True)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    hevy_id = Column(String, nullable=True)
    status = Column(String, nullable=False, default="written")
    digest = Column(String, nullable=True)
    updated_at = Column(DateTime, default=utcnow)


class GarminSetSync(Base):
    """Garmin kuvvet aktivitesinin setleri Hevy değerleriyle düzeltildi mi (aynı içerik tekrar yazılmasın)."""

    __tablename__ = "garmin_set_syncs"

    garmin_id = Column(String, primary_key=True)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    hevy_id = Column(String, nullable=True)
    digest = Column(String, nullable=True)
    changes = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=utcnow)


class StrengthWorkout(Base):
    """Kulvar'ın kendi kuvvet antrenmanı kaydı. Saatten gelen antrenmandan taslak olarak oluşur
    (planlanan değerlerle dolu), kullanıcı onaylayınca kesinleşir; Hevy kullanılıyorsa Hevy'den kopyalanır.

    status: live (telefonda sürüyor) | draft (onay bekliyor) | confirmed. source: kulvar | hevy (Hevy'den)."""

    __tablename__ = "strength_workouts"

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    start_time = Column(DateTime, nullable=False)  # yerel saat (aktivitelerle aynı)
    duration_s = Column(Float, nullable=True)
    title = Column(String, nullable=False)
    routine_id = Column(String, nullable=True)
    planned_workout_id = Column(String, nullable=True)
    garmin_activity_id = Column(String, nullable=True, index=True)  # Activity.external_id (source=garmin)
    hevy_id = Column(String, nullable=True)
    status = Column(String, nullable=False, default="draft")
    source = Column(String, nullable=False, default="kulvar")
    telegram_message_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    confirmed_at = Column(DateTime, nullable=True)

    sets = relationship("StrengthSet", order_by="(StrengthSet.position, StrengthSet.set_index)",
                        cascade="all, delete-orphan", back_populates="workout")


class StrengthSet(Base):
    """Bir kuvvet antrenmanının seti. position: hareketin sırası; aynı hareketin setleri aynı position'da."""

    __tablename__ = "strength_sets"

    id = Column(String, primary_key=True, default=_uuid)
    workout_id = Column(String, ForeignKey("strength_workouts.id"), nullable=False, index=True)
    position = Column(Integer, nullable=False)
    set_index = Column(Integer, nullable=False)
    title = Column(String, nullable=False)
    template_id = Column(String, nullable=True)  # hareket kimliği (ExerciseMedia.template_id)
    set_type = Column(String, nullable=False, default="normal")  # normal | warmup
    reps = Column(Integer, nullable=True)
    weight_kg = Column(Float, nullable=True)
    duration_s = Column(Float, nullable=True)
    planned_reps = Column(Integer, nullable=True)
    planned_weight_kg = Column(Float, nullable=True)
    planned_duration_s = Column(Float, nullable=True)
    watch_reps = Column(Integer, nullable=True)  # saatin saydığı (yalnızca ipucu)
    watch_weight_kg = Column(Float, nullable=True)
    wkt_step_index = Column(Integer, nullable=True)  # saatteki antrenman adımı (Garmin setiyle bağ)
    rest_s = Column(Integer, nullable=True)  # hareketin dinlenme süresi (canlı kayıt sayacı)
    done_at = Column(DateTime, nullable=True)  # canlı kayıtta ✓ zamanı

    workout = relationship("StrengthWorkout", back_populates="sets")


class ProgressionSuggestion(Base):
    """Kademeli yüklenme önerisi: bir hareketin hedefi son iki antrenmanda tamamlandı → bir sonraki adım.

    status: pending | applied | rejected | stale (rutin bu arada değişti)."""

    __tablename__ = "progression_suggestions"

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    routine_id = Column(String, nullable=False)
    exercise_key = Column(String, nullable=False)  # template_id ya da hareket adı
    title = Column(String, nullable=False)
    old_json = Column(Text, nullable=False)  # rutindeki setler
    new_json = Column(Text, nullable=False)  # önerilen setler
    status = Column(String, nullable=False, default="pending")
    telegram_message_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    decided_at = Column(DateTime, nullable=True)


class WorkoutFeedback(Base):
    """Antrenman sonrası algılanan zorluk (RPE 1–10) ve kısa not. ref: "act:<Activity.id>" ya da
    "sw:<StrengthWorkout.id>"; aynı antrenmanın Garmin/Strava/Hevy kopyaları için tek kayıt tutulur."""

    __tablename__ = "workout_feedback"
    __table_args__ = (UniqueConstraint("profile_id", "ref", name="uq_feedback_profile_ref"),)

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    ref = Column(String, nullable=False)
    date = Column(Date, nullable=False)
    title = Column(String, nullable=True)
    minutes = Column(Float, nullable=True)
    rpe = Column(Integer, nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow)


class MorningCheck(Base):
    """Sabah hazırlık kontrolü (profil başına günde bir): toparlanma düşük ve bugün zor koşu varsa öneri.

    status: none (sorun yok / zor antrenman yok) | sent (öneri gönderildi) | eased (kolaya çevrildi) | kept (plan korundu)."""

    __tablename__ = "morning_checks"
    __table_args__ = (UniqueConstraint("profile_id", "date", name="uq_morning_check_profile_date"),)

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    date = Column(Date, nullable=False)
    status = Column(String, nullable=False, default="none")
    planned_workout_id = Column(String, nullable=True)
    reasons_json = Column(Text, nullable=True)  # ["Uyku skoru 52 (ort. 74)", …]
    telegram_message_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    decided_at = Column(DateTime, nullable=True)


class LabResult(Base):
    """Tahlilden çıkarılan sayısal sonuç (seyir grafiği ve koç için). key: standart ad (ör. ferritin) ya da "other:<ad>"."""

    __tablename__ = "lab_results"

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    record_id = Column(String, ForeignKey("health_records.id"), nullable=True, index=True)
    key = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)  # rapordaki adı
    value = Column(Float, nullable=False)
    unit = Column(String, nullable=True)
    ref_low = Column(Float, nullable=True)
    ref_high = Column(Float, nullable=True)
    date = Column(Date, nullable=False)  # numune tarihi
    created_at = Column(DateTime, default=utcnow)
