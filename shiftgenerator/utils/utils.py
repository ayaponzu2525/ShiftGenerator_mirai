# utils.py
from django.utils import timezone
from shiftgenerator.models import ShiftSubmissionPeriod

def close_expired_periods():
    """auto_close_date を過ぎた募集期間を自動的に停止する"""
    now = timezone.now()
    qs = ShiftSubmissionPeriod.objects.filter(
        is_active=True,
        auto_close_date__isnull=False,
        auto_close_date__lte=now
    )
    # まとめて update で OK
    qs.update(is_active=False)
